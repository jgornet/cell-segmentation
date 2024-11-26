# hello.py

import os
import logging
import traceback
import uuid
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
app.config['CELERY_BROKER_URL'] = os.environ.get('RABBITMQ_URL', 'amqp://guest:guest@rabbitmq-service:5672/celery_vhost')
app.config['CELERY_RESULT_BACKEND'] = os.environ.get('REDIS_URL', 'redis://redis-service:6380/0')

# Initialize Celery
celery = Celery(app.name, broker=app.config['CELERY_BROKER_URL'], backend=app.config['CELERY_RESULT_BACKEND'])
celery.conf.update(app.config)

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
    "labuser": generate_password_hash("i2bh308sdf325u2iuh1922hd319ibfoiub82b82u329h8f48g28feubf38345673fojiw")
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
    return render_template('upload.html')

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
        presigned_post = s3_client.generate_presigned_post(
            Bucket=app.config['S3_BUCKET_INPUT'],
            Key=unique_filename,
            Fields={"Content-Type": content_type},
            Conditions=[
                ["content-length-range", 0, file_size + 1000000]  # Add some buffer
            ],
            ExpiresIn=3600
        )
        return jsonify({
            'url': presigned_post['url'],
            'fields': presigned_post['fields'],
            'fileName': unique_filename
        })
    except ClientError as e:
        return jsonify({'error': f'Error generating pre-signed POST data: {str(e)}'}), 500

@app.route('/complete_upload', methods=['POST'])
@auth.login_required
@limiter.limit("10 per minute")
def complete_upload():
    filename = request.json.get('fileName')
    if not filename:
        return jsonify({'error': 'No file name provided'}), 400

    # Enqueue processing task
    task = celery.send_task('worker.process_volume', args=[filename])
    return jsonify({'success': True, 'message': 'File upload completed and queued for processing', 'task_id': task.id})

@app.route('/status/<task_id>')
@auth.login_required
@limiter.limit("10 per minute")
def get_status(task_id):
    task = celery.AsyncResult(task_id)
    if task.state == 'PENDING':
        response = {
            'state': task.state,
            'status': 'Pending...'
        }
    elif task.state != 'FAILURE':
        response = {
            'state': task.state,
            'status': task.info.get('status', '')
        }
    else:
        response = {
            'state': task.state,
            'status': str(task.info)
        }
    return jsonify(response)

@app.route('/files')
@auth.login_required
@limiter.limit("10 per minute")
def list_files():
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
            task = celery.AsyncResult(filename)
            if task.state == 'PENDING':
                file_status[filename] = 'Pending'
            elif task.state == 'SUCCESS':
                file_status[filename] = 'Completed'
            elif task.state == 'FAILURE':
                file_status[filename] = 'Failed'
            else:
                file_status[filename] = 'Processing'
        
        return render_template('files.html', input_files=input_files, output_files=output_files, file_status=file_status)
    except ClientError as e:
        return jsonify({'error': f'S3 list error: {str(e)}'}), 500
    except Exception as e:
        return jsonify({'error': f'Unexpected error while listing files: {str(e)}\n{traceback.format_exc()}'}), 500

@app.route('/download/<bucket>/<filename>')
@auth.login_required
@limiter.limit("10 per minute")
def download_file(bucket, filename):
    if not s3_resource:
        return jsonify({'error': 'S3 client not initialized. Check AWS credentials.'}), 500
    
    try:
        if bucket not in [app.config['S3_BUCKET_INPUT'], app.config['S3_BUCKET_OUTPUT']]:
            abort(404)
        
        file_obj = s3_resource.Object(bucket, filename)
        file_stream = BytesIO()
        file_obj.download_fileobj(file_stream)
        file_stream.seek(0)
        
        return send_file(
            file_stream,
            as_attachment=True,
            attachment_filename=filename,
            mimetype='application/octet-stream'
        )
    except ClientError as e:
        if e.response['Error']['Code'] == "NoSuchKey":
            abort(404)
        else:
            return jsonify({'error': f'S3 download error: {str(e)}'}), 500
    except Exception as e:
        return jsonify({'error': f'Unexpected error during file download: {str(e)}'}), 500

@app.errorhandler(500)
def internal_server_error(error):
    return jsonify({'error': f'Internal Server Error: {str(error)}\n{traceback.format_exc()}'}), 500

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)