from json import loads
import os
from functools import lru_cache
import shutil
import logging

from celery import signals

import boto3
from skimage import io
import numpy as np
import voluseg
import h5py
from pyspark.sql import SparkSession

from config import celery


@celery.task(bind=True, queue='tasks')
def process_volume(self, url, parameters_json=None):
    self.update_state(task_id=url, state='STARTED')
    
    download_url = url

    print(f"Downloading {download_url}")
    download_tif(download_url)
    print(f"Downloaded {download_url}")

    print("Converting tif to h5")
    tif_to_h5("/data/input.tif")
    print("Converted tif to h5")

    print("Generating traces")
    generate_output(parameters_json)
    print("Generated traces")

    print("Uploading traces")
    fn = os.path.splitext(url)[0]
    upload_url = f"{fn}.zip"
    upload_output(upload_url)
    print("Uploaded output")

    clean_directory()


@signals.worker_process_init.connect
@lru_cache(maxsize=None)
def get_spark(**kwargs):
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
        .config("spark.executor.extraJavaOptions", "-XX:+UseG1GC -XX:+PrintGCDetails -XX:+PrintGCTimeStamps")
        .config("spark.driver.extraJavaOptions", "-XX:+UseG1GC -XX:+PrintGCDetails -XX:+PrintGCTimeStamps")
        .appName("voluseg_processing")
        .getOrCreate()
    )
    
    spark.sparkContext.setLogLevel("WARN")
    return spark


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
    print("Starting generate_output")

    parameters = voluseg.parameter_dictionary()

    if custom_parameters:
        print("Applying custom parameters")
        c_parameters = loads(custom_parameters)
        for key, value in c_parameters.items():
            parameters[key] = value

    else:
        print("Using default parameters")
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

    print("Processing parameters with step0")
    voluseg.step0_process_parameters(parameters)

    with open("/data/output/parameters.json", "r") as f:
        parameters = loads(f.read())

    # Process volumes sequentially for step1
    print("Starting step1_process_volumes sequentially")
    input_dir = parameters["dir_input"]
    volume_files = sorted([f for f in os.listdir(input_dir) if f.endswith('.h5')])
    
    for volume_file in volume_files:
        print(f"Processing volume {volume_file} in step1")
        volume_path = os.path.join(input_dir, volume_file)
        # Create a temporary parameters dict for this volume
        vol_params = parameters.copy()
        vol_params["volume_files"] = [volume_file]
        
        try:
            voluseg.step1_process_volumes(vol_params)
            print(f"Completed processing {volume_file} in step1")
        except Exception as e:
            print(f"Error processing {volume_file} in step1: {str(e)}")
            raise
        
        # Force cleanup after each volume
        import gc
        gc.collect()
        
        # Get Spark context and clear cache
        spark = get_spark()
        spark.catalog.clearCache()
        
        print(f"Cleaned up after processing {volume_file}")

    print("Completed step1")

    # Process volumes sequentially for step2
    print("Starting step2_align_volumes sequentially")
    processed_volumes = sorted([f for f in os.listdir(parameters["dir_output"]) if f.startswith('volume')])
    
    for volume in processed_volumes:
        print(f"Aligning volume {volume} in step2")
        # Create a temporary parameters dict for this volume
        vol_params = parameters.copy()
        vol_params["volume_names"] = [volume]
        
        try:
            voluseg.step2_align_volumes(vol_params)
            print(f"Completed aligning {volume} in step2")
        except Exception as e:
            print(f"Error aligning {volume} in step2: {str(e)}")
            raise
        
        # Force cleanup after each volume
        gc.collect()
        spark = get_spark()
        spark.catalog.clearCache()
        
        print(f"Cleaned up after aligning {volume}")

    print("Completed step2")

    # Continue with the remaining steps as they don't use Spark
    parameters["volume_names"] = np.array(parameters["volume_names"])
    print("Starting step3_mask_volumes")
    voluseg.step3_mask_volumes(parameters)
    print("Completed step3")

    print("Starting step4_detect_cells")
    voluseg.step4_detect_cells(parameters)
    print("Completed step4")

    print("Starting step5_clean_cells")
    voluseg.step5_clean_cells(parameters)
    print("Completed step5")


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