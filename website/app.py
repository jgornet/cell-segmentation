# app.py

import os
import queue
import threading
import time
from flask import Flask, request, render_template, send_file, abort, jsonify
from flask_httpauth import HTTPBasicAuth
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from dataprocessing import process_data
from datetime import datetime
from flask import jsonify, request
import shutil
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import pyspark
from pyspark.sql import SparkSession
import findspark
findspark.init()

app = Flask(__name__)
auth = HTTPBasicAuth()
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "60 per hour"],
    storage_uri="memory://"
)

# Configuration
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['PROCESSED_FOLDER'] = 'processed'

# Create a processing queue
processing_queue = queue.Queue()

# List to keep track of the queue order
queue_order = []

# Lock for thread-safe operations on queue_order
queue_lock = threading.Lock()

# Dictionary to track file processing status
file_status = {}

# User credentials (replace with your own secure method)
users = {
    "labuser": generate_password_hash("i2bh308sdf325u2iuh1922hd319ibfoiub82b82u329h8f48g28feubf38345673fojiw")
}

def remove_directory(path):
    for root, dirs, files in os.walk(path, topdown=False):
        for name in files:
            os.remove(os.path.join(root, name))
        for name in dirs:
            os.rmdir(os.path.join(root, name))
    os.rmdir(path)

@auth.verify_password
def verify_password(username, password):
    if username in users and check_password_hash(users.get(username), password):
        return username
    # Add a small delay to slow down brute force attempts
    time.sleep(0.5)
    return None

@auth.error_handler
def auth_error(status):
    return "Access Denied", status

def update_queue_positions():
    with queue_lock:
        for i, filename in enumerate(queue_order):
            if file_status[filename] == 'Queued':
                file_status[filename] = f'Queued (Position: {i+1})'

def process_file(filename):
    timestampstr = str(datetime.now()).replace(" ", "_").replace(":", "_").replace(".", "_")
    source_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    dest_path = os.path.join(app.config['PROCESSED_FOLDER'], "FINISHED"+timestampstr+filename)
    work_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'temp_' + timestampstr + filename)

    os.makedirs(work_dir, exist_ok=True)

    try:
        file_status[filename] = 'Processing'
        process_data(source_path, work_dir, dest_path)
        file_status[filename] = 'Completed'
    except Exception as e:
        file_status[filename] = f'Error: {str(e)}'
    finally:
        # Clean up the temporary working directory
        if os.path.exists(work_dir):
            try:
                remove_directory(work_dir)
            except Exception as e:
                print(f"Error removing directory {work_dir}: {e}")

    # Remove the original uploaded file
    #os.remove(source_path)

def process_queue():
    while True:
        filename = processing_queue.get()
        with queue_lock:
            queue_order.remove(filename)
        update_queue_positions()
        process_file(filename)
        processing_queue.task_done()

# Start the processing thread
threading.Thread(target=process_queue, daemon=True).start()

@app.route('/')
@auth.login_required
@limiter.limit("10 per minute")
def upload_form():
    return render_template('upload.html')

@app.route('/upload', methods=['GET', 'POST'])
@auth.login_required
@limiter.limit("10 per minute")
def upload_file():
    if request.method == 'POST':
        if 'file' not in request.files:
            return jsonify({'error': 'No file part'})
        file = request.files['file']
        if file.filename == '':
            return jsonify({'error': 'No selected file'})
        if file:
            filename = secure_filename(file.filename)
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            # Add the file to the processing queue
            processing_queue.put(filename)
            with queue_lock:
                queue_order.append(filename)
            file_status[filename] = f'Queued (Position: {len(queue_order)})'
            update_queue_positions()
            return jsonify({'success': True, 'message': 'File uploaded and queued for processing'})
    return render_template('upload.html')

@app.route('/status')
@auth.login_required
@limiter.limit("10 per minute")
def get_status():
    return render_template('status.html', file_status=file_status)

@app.route('/download/<filename>')
@auth.login_required
@limiter.limit("10 per minute")
def download_file(filename):
    filename = secure_filename(filename)
    file_path = os.path.join(app.config['PROCESSED_FOLDER'], filename)
    if os.path.isfile(file_path):
        return send_file(file_path, as_attachment=True)
    else:
        abort(404)

@app.route('/files')
@auth.login_required
@limiter.limit("10 per minute")
def list_files():
    processed_files = os.listdir(app.config['PROCESSED_FOLDER'])
    return render_template('download.html', files=processed_files, status=file_status)


@app.route('/uploaded_files')
@auth.login_required
@limiter.limit("10 per minute")
def list_uploaded_files():
    uploaded_files = [f for f in os.listdir(app.config['UPLOAD_FOLDER']) if os.path.isfile(os.path.join(app.config['UPLOAD_FOLDER'], f))]
    return render_template('uploaded_files.html', files=uploaded_files)

@app.route('/download_uploaded/<filename>')
@auth.login_required
@limiter.limit("10 per minute")
def download_uploaded_file(filename):
    filename = secure_filename(filename)
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    if os.path.isfile(file_path):
        return send_file(file_path, as_attachment=True)
    else:
        abort(404)



spark = SparkSession.builder \
    .master("local[*]") \
    .config("spark.driver.maxResultSize", "0") \
    .config("spark.executor.memory", "70g") \
    .config("spark.driver.memory", "70g") \
    .config("spark.memory.offHeap.enabled", True) \
    .config("spark.memory.offHeap.size","70g") \
    .config("spark.cores.max", "23") \
    .appName("sampleCodeForReference") \
    .getOrCreate()

if __name__ == '__main__':
    app.run(debug=False, host='192.168.1.112')#, ssl_context=('ssl/cert.pem', 'ssl/key.pem'))
