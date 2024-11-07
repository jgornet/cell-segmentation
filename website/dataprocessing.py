# dataprocessing.py

import findspark
findspark.init()
import pyspark
from pyspark.sql import SparkSession
import os
import h5py
import numpy as np
from skimage import io
import tifffile as tif
import tarfile
import gzip
import shutil
import voluseg
import zipfile


def uncompress_file(file_path, extract_path):
    """Uncompress the uploaded file."""
    if file_path.endswith('.tar.gz'):
        with tarfile.open(file_path, 'r:gz') as tar:
            tar.extractall(path=extract_path)
    elif file_path.endswith('.gz'):
        output_path = os.path.join(extract_path, os.path.basename(file_path)[:-3])
        with gzip.open(file_path, 'rb') as f_in:
            with open(output_path, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)
    else:
        # If it's not compressed, just copy it
        shutil.copy(file_path, extract_path)


def process_tiff(input_path, output_path):
    """Process the TIFF file as in test0.py."""
    data = io.imread(input_path)
    
    if data is None or len(data) == 0:
        raise ValueError(f"Failed to read data from {input_path}")
    
    print(f"Original data shape: {np.shape(data)}")
    
    # Optionally crop the data (commented out as in test0.py)
    # data_cropped = data[:,:,:]
    data_cropped = data  # Using full data as in test0.py
    print(f"Cropped data shape: {np.shape(data_cropped)}")
    
    dir_to_save = output_path
    nfr_per_vol = 25  # number of slices

    total_num_frames = len(data_cropped)
    prev_frame = 0
    for volume, next_frame in enumerate(range(nfr_per_vol, total_num_frames + nfr_per_vol, nfr_per_vol)):
        hf = h5py.File(os.path.join(dir_to_save, f'volume{1000 + volume}.h5'), 'w')
        dset = hf.create_dataset('default', data=data_cropped[prev_frame:next_frame])
        hf.close()
        prev_frame = next_frame

    print(f"Processed {total_num_frames} frames into {volume + 1} volumes")


def run_voluseg_pipeline(input_path, output_path):
    """Run the VoluSeg pipeline as in test.py."""
    #parameters = voluseg.parameter_dictionary()
    parameters = voluseg.load_parameters("parameters.json")
    parameters['dir_input'] = input_path
    parameters['dir_output'] = output_path
    parameters['dir_ants'] = "install/bin"
    # Set other parameters as needed

    voluseg.step0_process_parameters(parameters)
    voluseg.step1_process_volumes(parameters)
    voluseg.step2_align_volumes(parameters)
    voluseg.step3_mask_volumes(parameters)
    voluseg.step4_detect_cells(parameters)
    voluseg.step5_clean_cells(parameters)


def compress_output(input_path, output_file):
    """Compress the output folder."""
    output_file_with_extension = f"{output_file}.zip"
    with zipfile.ZipFile(output_file_with_extension, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(input_path):
            for file in files:
                file_path = os.path.join(root, file)
                arcname = os.path.relpath(file_path, input_path)
                zipf.write(file_path, arcname)


def process_data(input_file, work_dir, output_file):
    """Main function to process the data."""
    # Create temporary directories
    extract_dir = os.path.join(work_dir, 'extracted')
    processed_dir = 'input' #os.path.join(work_dir, 'processed')
    voluseg_output_dir = 'output' #os.path.join(work_dir, 'voluseg_output')

    os.makedirs(extract_dir, exist_ok=True)
    os.makedirs(processed_dir, exist_ok=True)
    os.makedirs(voluseg_output_dir, exist_ok=True)

    # Step 1: Uncompress
    uncompress_file(input_file, extract_dir)

    # Step 2: Process TIFF
    tiff_files = [f for f in os.listdir(extract_dir) if f.endswith('.tif')]
    tiff_file = tiff_files[0]
    
    process_tiff(os.path.join(extract_dir, tiff_file), processed_dir)

    # Step 3: Run VoluSeg pipeline
    run_voluseg_pipeline(processed_dir, voluseg_output_dir)

    # Step 4: Compress output
    # output_file = os.path.join(output_file, output_file)
    
    compress_output(voluseg_output_dir, output_file)

    # Clean up temporary directories
    shutil.rmtree(extract_dir)
    shutil.rmtree(processed_dir)
    shutil.rmtree(voluseg_output_dir)

