# hello.py

import os
import logging
import traceback
import math
from flask import Flask, request, render_template, send_file, abort, jsonify, url_for
from flask_httpauth import HTTPBasicAuth
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from celery import Celery
import time
from io import BytesIO
import uuid
from flask import redirect
from celery.app.control import Inspect
from urllib.parse import quote

app = Flask(__name__)
auth = HTTPBasicAuth()
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "60 per hour"],
    storage_uri="memory://"
)

# Configuration
app.config['S3_BUCKET_INPUT'] = os.environ.get('S3_BUCKET_INPUT', 'voluseg-input')
app.config['S3_BUCKET_OUTPUT'] = os.environ.get('S3_BUCKET_OUTPUT', 'voluseg-output')
app.config['CELERY_BROKER_URL'] = os.environ.get('RABBITMQ_URL', 'redis://redis-service:6380/0')
app.config['CELERY_RESULT_BACKEND'] = os.environ.get('REDIS_URL', 'redis://redis-service:6380/0')

RABBITMQ_URL= os.environ.get('RABBITMQ_URL', 'amqp://guest:guest@rabbitmq-service:5672/celery_vhost')
REDIS_URL = os.environ.get('REDIS_URL', 'redis://redis-service:6380/0')

# Initialize Celery
# celery = Celery(app.name, broker=app.config['CELERY_BROKER_URL'], backend=app.config['CELERY_RESULT_BACKEND'])
# celery.conf.update(app.config)
# celery.conf.result_backend = app.config['CELERY_RESULT_BACKEND']
# celery.conf.task_default_queue = 'tasks'

celery = Celery(
    "tasks",
    broker=RABBITMQ_URL,
    backend=REDIS_URL,
    task_compression="gzip",
    task_track_started=True,
    task_acks_late=False,
    task_acks_on_failure_or_timeout=True,
    worker_cancel_long_running_tasks_on_connection_loss=False,
    worker_prefetch_multiplier=1,
    result_extended=True,
    broker_connection_max_retries=None,
    broker_connection_timeout=72 * 60 * 60,
)
celery.conf.broker_transport_options = {"visibility_timeout": 24 * 60 * 60}

# S3 client and resource
try:
    s3_client = boto3.client(
        "s3",
        aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
    )
    s3_resource = boto3.resource(
        "s3",
        aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
    )
except NoCredentialsError:
    s3_client = None
    s3_resource = None

# User credentials (replace with your own secure method)
users = {
    "labuser": generate_password_hash(os.environ['PASSWORD'])
}

@auth.verify_password
def verify_password(username, password):
    if username in users and check_password_hash(users.get(username), password):
        return username
    time.sleep(0.5)
    return None

@auth.error_handler
def auth_error(status):
    return "Access Denied", status

@app.route('/')
@auth.login_required
@limiter.limit("10 per minute")
def upload_form():
    if not s3_resource:
        return jsonify({'error': 'S3 resource not initialized. Check AWS credentials.'}), 500
    
    try:
        input_bucket = s3_resource.Bucket(app.config['S3_BUCKET_INPUT'])
        output_bucket = s3_resource.Bucket(app.config['S3_BUCKET_OUTPUT'])
        
        input_files = [obj.key for obj in input_bucket.objects.all()]
        output_files = [obj.key for obj in output_bucket.objects.all()]
        
        # Get processing status for input files
        file_status = {}
        for filename in input_files:
            # Check if there's a corresponding output file
            filename_base = os.path.splitext(filename)[0]
            has_output = any(out_file.startswith(filename_base) for out_file in output_files)
            
            task = celery.AsyncResult(filename)
            
            if task.state == 'SUCCESS' or has_output:
                file_status[filename] = 'Completed'
            elif task.state == 'STARTED':
                file_status[filename] = 'Processing'
            elif task.state == 'PENDING':
                file_status[filename] = 'Pending'
            elif task.state == 'FAILURE':
                file_status[filename] = 'Failed'
            else:
                file_status[filename] = f'Unknown ({task.state})'
        
        # Get currently running tasks
        running_tasks = []
        celery_inspect_error = None
        try:
            i = Inspect(app=celery)
            active_tasks = i.active()
            
            if active_tasks:
                for worker, tasks in active_tasks.items():
                    for task in tasks:
                        if task.get('name') == 'worker.process_volume':
                            running_tasks.append(task.get('args', [''])[0])
        except Exception as e:
            celery_inspect_error = str(e)
            app.logger.error(f"Error inspecting Celery tasks: {str(e)}")
        
        return render_template('upload.html', 
                           input_files=input_files, 
                           output_files=output_files, 
                           file_status=file_status,
                           running_tasks=running_tasks,
                           celery_inspect_error=celery_inspect_error)
    except ClientError as e:
        return jsonify({'error': f'S3 list error: {str(e)}'}), 500
    except Exception as e:
        return jsonify({'error': f'Unexpected error while listing files: {str(e)}\n{traceback.format_exc()}'}), 500

@app.route('/get_upload_url', methods=['POST'])
@auth.login_required
@limiter.limit("10 per minute")
def get_upload_url():
    if not s3_client:
        return jsonify({'error': 'S3 client not initialized. Check AWS credentials.'}), 500

    file_name = request.json.get('fileName')
    content_type = request.json.get('contentType')
    file_size = int(request.json.get('fileSize'))
    if not file_name or not content_type or not file_size:
        return jsonify({'error': 'File name, content type, and file size are required'}), 400

    # Generate a unique file name to avoid overwrites
    unique_filename = f"{uuid.uuid4()}-{secure_filename(file_name)}"

    try:
        # Initiate multipart upload
        multipart_upload = s3_client.create_multipart_upload(
            Bucket=app.config['S3_BUCKET_INPUT'],
            Key=unique_filename,
            ContentType="image/tiff"
        )
        
        # Calculate the number of parts (increasing part size for large files)
        part_size = 20 * 1024 * 1024  # Increased to 20MB
        total_parts = math.ceil(file_size / part_size)
        
        # If file is very large, increase part size to keep parts under 1000 (S3 limit)
        while total_parts > 1000:
            part_size *= 2
            total_parts = math.ceil(file_size / part_size)

        # Generate presigned URLs for each part
        presigned_urls = []
        for part_number in range(1, total_parts + 1):
            presigned_url = s3_client.generate_presigned_url(
                'upload_part',
                Params={
                    'Bucket': app.config['S3_BUCKET_INPUT'],
                    'Key': unique_filename,
                    'UploadId': multipart_upload['UploadId'],
                    'PartNumber': part_number,
                },
                ExpiresIn= 48 * 3600  # Increased to 48 hours
            ) 
            presigned_urls.append(presigned_url)

        return jsonify({
            'uploadId': multipart_upload['UploadId'],
            'urls': presigned_urls,
            'fileName': unique_filename,
            'partSize': part_size,
            'totalParts': total_parts
        })
    except ClientError as e:
        return jsonify({'error': f'Error initiating multipart upload: {str(e)}'}), 500

@app.route('/complete_multipart_upload', methods=['POST'])
@auth.login_required
@limiter.limit("10 per minute")
def complete_multipart_upload():
    data = request.json
    if not data or 'fileName' not in data or 'uploadId' not in data or 'parts' not in data:
        return jsonify({'error': 'Missing required data'}), 400

    try:
        response = s3_client.complete_multipart_upload(
            Bucket=app.config['S3_BUCKET_INPUT'],
            Key=data['fileName'],
            UploadId=data['uploadId'],
            MultipartUpload={'Parts': data['parts']}
        )
        # Enqueue processing task with optional parameters
        parameters = data.get('parameters')  # Will be None if not provided
        task = celery.send_task(
            'worker.process_volume',
            args=[data['fileName'], parameters],
            task_id=data['fileName'],
            queue='tasks'
        )
        return jsonify({'success': True, 'message': 'File upload completed and queued for processing', 'task_id': task.id})
    except ClientError as e:
        return jsonify({'error': f'Error completing multipart upload: {str(e)}'}), 500

@app.route('/download/<bucket>/<filename>')
@auth.login_required
@limiter.limit("10 per minute")
def download_file(bucket, filename):
    if not s3_client:
        return jsonify({'error': 'S3 client not initialized. Check AWS credentials.'}), 500
    
    try:
        if bucket not in [app.config['S3_BUCKET_INPUT'], app.config['S3_BUCKET_OUTPUT']]:
            abort(404)
        
        # Generate a pre-signed URL
        url = s3_client.generate_presigned_url(
            'get_object',
            Params={
                'Bucket': bucket,
                'Key': filename,
                'ResponseContentDisposition': f'attachment; filename="{quote(filename)}"'
            },
            ExpiresIn=3600  # URL expires in 1 hour
        )
        
        # Redirect the user to the pre-signed URL
        return redirect(url)

    except ClientError as e:
        app.logger.error(f"Error generating pre-signed URL: {str(e)}")
        return jsonify({'error': f'Error generating download link: {str(e)}'}), 500
    except Exception as e:
        app.logger.error(f"Unexpected error: {str(e)}")
        return jsonify({'error': f'Unexpected error: {str(e)}'}), 500

@app.errorhandler(500)
def internal_server_error(error):
    return jsonify({'error': f'Internal Server Error: {str(error)}\n{traceback.format_exc()}'}), 500

@app.route('/task_error/<task_id>')
@auth.login_required
@limiter.limit("10 per minute")
def get_task_error(task_id):
    try:
        task = celery.AsyncResult(task_id)
        if task.state == 'FAILURE':
            # Return the error information
            return jsonify({
                'error': str(task.result),  # The error message
                'traceback': task.traceback  # The full traceback
            })
        return jsonify({'error': 'Task has not failed'}), 404
    except Exception as e:
        return jsonify({'error': f'Error retrieving task error: {str(e)}'}), 500

# Add new API endpoints
@app.route('/api/upload', methods=['POST'])
@auth.login_required
@limiter.limit("10 per minute")
def api_upload():
    if not s3_client:
        return jsonify({'error': 'S3 client not initialized'}), 500
        
    if 'file_name' not in request.json or 'file_size' not in request.json:
        return jsonify({'error': 'file_name and file_size required'}), 400
        
    file_name = request.json['file_name']
    file_size = int(request.json['file_size'])
    parameters = request.json.get('parameters')  # Optional parameters
    
    try:
        # Generate unique filename
        unique_filename = f"{uuid.uuid4()}-{secure_filename(file_name)}"
        
        # Initiate multipart upload
        multipart_upload = s3_client.create_multipart_upload(
            Bucket=app.config['S3_BUCKET_INPUT'],
            Key=unique_filename,
            ContentType="image/tiff"
        )
        
        # Calculate parts
        part_size = 5 * 1024 * 1024  # 5MB
        total_parts = math.ceil(file_size / part_size)
        
        # Generate presigned URLs
        presigned_urls = []
        for part_number in range(1, total_parts + 1):
            url = s3_client.generate_presigned_url(
                'upload_part',
                Params={
                    'Bucket': app.config['S3_BUCKET_INPUT'],
                    'Key': unique_filename,
                    'UploadId': multipart_upload['UploadId'],
                    'PartNumber': part_number
                },
                ExpiresIn= 48 * 3600  # 24 hours instead of 1 hour
            )
            presigned_urls.append(url)
            
        return jsonify({
            'upload_id': multipart_upload['UploadId'],
            'file_name': unique_filename,
            'urls': presigned_urls,
            'part_size': part_size
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/complete_upload', methods=['POST'])
@auth.login_required
@limiter.limit("10 per minute")
def api_complete_upload():
    if not request.json:
        return jsonify({'error': 'Invalid request'}), 400
        
    try:
        # Complete multipart upload
        response = s3_client.complete_multipart_upload(
            Bucket=app.config['S3_BUCKET_INPUT'],
            Key=request.json['file_name'],
            UploadId=request.json['upload_id'],
            MultipartUpload={'Parts': request.json['parts']}
        )
        
        # Start processing
        parameters = request.json.get('parameters')
        task = celery.send_task(
            'worker.process_volume',
            args=[request.json['file_name'], parameters],
            task_id=request.json['file_name'],
            queue='tasks'
        )
        
        return jsonify({
            'task_id': task.id,
            'status': 'processing'
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/status/<task_id>')
@auth.login_required
@limiter.limit("10 per minute")
def api_task_status(task_id):
    try:
        task = celery.AsyncResult(task_id)
        
        if task.state == 'FAILURE':
            return jsonify({
                'status': 'failed',
                'error': str(task.result),
                'traceback': task.traceback
            })
            
        # Check for output file
        output_key = f"{os.path.splitext(task_id)[0]}.zip"
        try:
            s3_client.head_object(
                Bucket=app.config['S3_BUCKET_OUTPUT'],
                Key=output_key
            )
            # Generate download URL if output exists
            url = s3_client.generate_presigned_url(
                'get_object',
                Params={
                    'Bucket': app.config['S3_BUCKET_OUTPUT'],
                    'Key': output_key
                },
                ExpiresIn=3600
            )
            return jsonify({
                'status': 'completed',
                'download_url': url
            })
        except ClientError:
            # Output file doesn't exist yet
            return jsonify({
                'status': task.state.lower()
            })
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)