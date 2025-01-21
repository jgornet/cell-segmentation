from json import loads
import os
from functools import lru_cache
import shutil
import logging
import psutil
import gc
import sys
import traceback
from celery import signals
import boto3
from skimage import io
import numpy as np
import voluseg
import h5py
from pyspark.sql import SparkSession
from config import celery


# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def log_memory_usage():
    process = psutil.Process(os.getpid())
    mem_info = process.memory_info()
    logger.info(f"Memory Usage - RSS: {mem_info.rss / 1024 / 1024:.2f}MB, VMS: {mem_info.vms / 1024 / 1024:.2f}MB")

def log_spark_status(spark):
    try:
        logger.info(f"Active Spark Sessions: {SparkSession.getActiveSession()}")
        logger.info(f"Spark UI URL: {spark.sparkContext.uiWebUrl}")
        logger.info(f"Available Executors: {len(spark.sparkContext.statusTracker().getExecutorInfos())}")
        logger.info(f"Default Parallelism: {spark.sparkContext.defaultParallelism}")
    except Exception as e:
        logger.error(f"Error getting Spark status: {str(e)}")

@celery.task(bind=True, queue='tasks')
def process_volume(self, url, parameters_json=None):
    try:
        logger.info(f"Starting process_volume task for {url}")
        log_memory_usage()
        
        self.update_state(task_id=url, state='STARTED')
        download_url = url

        logger.info(f"Downloading {download_url}")
        download_tif(download_url)
        logger.info(f"Downloaded {download_url}")
        log_memory_usage()

        logger.info("Converting tif to h5")
        tif_to_h5("/data/input.tif")
        logger.info("Converted tif to h5")
        log_memory_usage()

        logger.info("Starting generate_output")
        generate_output(parameters_json)
        logger.info("Completed generate_output")
        log_memory_usage()

        logger.info("Uploading traces")
        fn = os.path.splitext(url)[0]
        upload_url = f"{fn}.zip"
        upload_output(upload_url)
        logger.info("Uploaded output")

        clean_directory()
        logger.info("Task completed successfully")
        
    except Exception as e:
        logger.error(f"Task failed with error: {str(e)}")
        logger.error(f"Full traceback: {traceback.format_exc()}")
        # Force garbage collection
        gc.collect()
        raise

@signals.worker_process_init.connect
@lru_cache(maxsize=None)
def get_spark(**kwargs):
    logger.info("Initializing Spark session")
    try:
        spark = (
            SparkSession.builder.master("local[*]")
            .config("spark.driver.maxResultSize", "10g")
            .config("spark.executor.memory", "50g")
            .config("spark.driver.memory", "50g")
            .config("spark.memory.offHeap.enabled", True)
            .config("spark.memory.offHeap.size", "20g")
            .config("spark.serializer", "org.apache.spark.serializer.KryoSerializer")
            .config("spark.kryoserializer.buffer.max", "512m")
            .config("spark.kryoserializer.buffer", "128m")
            .config("spark.memory.fraction", "0.6")
            .config("spark.memory.storageFraction", "0.4")
            .config("spark.shuffle.file.buffer", "1m")
            .config("spark.shuffle.spill.compress", "true")
            .config("spark.shuffle.compress", "true")
            .config("spark.rdd.compress", "true")
            .config("spark.default.parallelism", "4")
            .config("spark.sql.shuffle.partitions", "4")
            .config("spark.python.worker.memory", "4g")
            .config("spark.python.worker.reuse", "true")
            .config("spark.executor.extraJavaOptions", "-XX:+UseG1GC -XX:+PrintGCDetails -XX:+PrintGCTimeStamps -verbose:gc")
            .config("spark.driver.extraJavaOptions", "-XX:+UseG1GC -XX:+PrintGCDetails -XX:+PrintGCTimeStamps -verbose:gc")
            .appName("voluseg_processing")
            .getOrCreate()
        )
        
        spark.sparkContext.setLogLevel("WARN")
        
        logger.info("Spark session initialized successfully")
        log_spark_status(spark)
        log_memory_usage()
        
        return spark
        
    except Exception as e:
        logger.error(f"Failed to initialize Spark: {str(e)}")
        logger.error(f"Full traceback: {traceback.format_exc()}")
        raise

def download_tif(url: str):
    s3 = boto3.resource(
        "s3",
        aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
    )
    print(f"Downloading {url}")
    bucket = s3.Bucket("voluseg-input")
    bucket.download_file(url, "/data/input.tif")
    print(f"Downloaded {url}")


def tif_to_h5(tif_path):
    # Read the tif file
    data = io.imread(tif_path)
    nfr_per_vol = 25
    prev_frame = 0
    total_num_frames = len(data)
    os.makedirs("/data/h5_volume", exist_ok=True)

    for volume, next_frame in enumerate(
        np.arange(nfr_per_vol, total_num_frames + nfr_per_vol, nfr_per_vol)
    ):
        h5_path = f"/data/h5_volume/volume{1000 + volume}.h5"
        with h5py.File(h5_path, "w") as h5_file:
            h5_file.create_dataset(
                "default", data=data[prev_frame:next_frame]
            )  # write the data to hdf5 file
        prev_frame = next_frame


def generate_output(custom_parameters=None):
    logger.info("Starting generate_output")
    log_memory_usage()

    try:
        parameters = voluseg.parameter_dictionary()
        logger.info("Got parameter dictionary")

        if custom_parameters:
            logger.info("Applying custom parameters")
            c_parameters = loads(custom_parameters)
            for key, value in c_parameters.items():
                parameters[key] = value
                logger.info(f"Set parameter {key} = {value}")

        else:
            logger.info("Using default parameters")
            parameters["registration"] = "high"
            parameters["diam_cell"] = 5.0
            parameters["f_volume"] = 1.0
            parameters["t_section"] = 0.04
            parameters["ds"] = 1
            parameters["res_x"] = 0.585
            parameters["res_y"] = 0.585
            parameters["res_z"] = 25

        parameters["dir_ants"] = "/opt/ANTs/bin"
        parameters["dir_input"] = "/data/h5_volume"
        parameters["dir_output"] = "/data/output"

        logger.info("Processing parameters with step0")
        voluseg.step0_process_parameters(parameters)
        logger.info("Completed step0")
        log_memory_usage()

        with open("/data/output/parameters.json", "r") as f:
            parameters = loads(f.read())
        logger.info("Loaded processed parameters")

        logger.info("Starting step1_process_volumes")
        log_memory_usage()
        voluseg.step1_process_volumes(parameters)
        logger.info("Completed step1")
        log_memory_usage()

        logger.info("Starting step2_align_volumes")
        voluseg.step2_align_volumes(parameters)
        logger.info("Completed step2")
        log_memory_usage()

        parameters["volume_names"] = np.array(parameters["volume_names"])
        
        logger.info("Starting step3_mask_volumes")
        voluseg.step3_mask_volumes(parameters)
        logger.info("Completed step3")
        log_memory_usage()

        logger.info("Starting step4_detect_cells")
        voluseg.step4_detect_cells(parameters)
        logger.info("Completed step4")
        log_memory_usage()

        logger.info("Starting step5_clean_cells")
        voluseg.step5_clean_cells(parameters)
        logger.info("Completed step5")
        log_memory_usage()

    except Exception as e:
        logger.error(f"Error in generate_output: {str(e)}")
        logger.error(f"Full traceback: {traceback.format_exc()}")
        # Force garbage collection before raising
        gc.collect()
        raise


def upload_output(url: str):
    # Replace .hdf5 extension with .zip
    # url = url.replace('.hdf5', '.zip')
    
    # Create zip file of the output directory
    shutil.make_archive('/data/output_archive', 'zip', '/data/output')
    
    # Upload to S3
    s3 = boto3.resource(
        "s3",
        aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
    )
    bucket = s3.Bucket("voluseg-output")
    bucket.upload_file('/data/output_archive.zip', url)
    
    # Clean up the zip file
    os.remove('/data/output_archive.zip')


def clean_directory():
    os.remove("/data/input.tif")
    shutil.rmtree("/data/h5_volume")
    shutil.rmtree("/data/output")