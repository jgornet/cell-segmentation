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
        .config("spark.driver.maxResultSize", "0")
        .config("spark.executor.memory", "70g")
        .config("spark.driver.memory", "70g")
        .config("spark.memory.offHeap.enabled", True)
        .config("spark.memory.offHeap.size", "70g")
        .appName("sampleCodeForReference")
        .getOrCreate()
    )
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
    

    # If custom parameters were provided, overwrite the generated parameters file

    parameters = voluseg.parameter_dictionary()

    # if custom_parameters:
    #     c_parameters = loads(custom_parameters)
    #     for key, value in c_parameters.items():
    #         parameters[key] = value

    # else:
    #     parameters["registration"] = "high"
    #     parameters["diam_cell"] = 5.0
    #     parameters["f_volume"] = 1.0
    #     parameters["t_section"] = 0.04
    #     parameters["ds"] = 1
    #     parameters["res_x"] = 0.585
    #     parameters["res_y"] = 0.585
    #     parameters["res_z"] = 25

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

    voluseg.step0_process_parameters(parameters)

    # Now read the parameters file (either custom or generated)
    with open("/data/output/parameters.json", "r") as f:
        parameters = loads(f.read())

    # sets directory parameters in json
    

    # Continue with processing
    voluseg.step1_process_volumes(parameters)
    voluseg.step2_align_volumes(parameters)
    parameters["volume_names"] = np.array(parameters["volume_names"])
    voluseg.step3_mask_volumes(parameters)
    voluseg.step4_detect_cells(parameters)
    voluseg.step5_clean_cells(parameters)


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