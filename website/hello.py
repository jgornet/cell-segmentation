# hello.py

import os
from flask import Flask, request, render_template, send_file, abort, jsonify, url_for
from flask_httpauth import HTTPBasicAuth
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import boto3
from botocore.exceptions import ClientError
from celery import Celery
import time

app = Flask(__name__)
auth = HTTPBasicAuth()
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "60 per hour"],
    storage_uri="memory://"
)

# Configuration
app.config['S3_BUCKET_INPUT'] = 'voluseg-input'
app.config['S3_BUCKET_OUTPUT'] = 'voluseg-output'
app.config['CELERY_BROKER_URL'] = os.environ.get('RABBITMQ_URL', 'amqp://guest:guest@rabbitmq-service:5672/celery_vhost')
app.config['CELERY_RESULT_BACKEND'] = os.environ.get('REDIS_URL', 'redis://redis-service:6380/0')

# Initialize Celery
celery = Celery(app.name, broker=app.config['CELERY_BROKER_URL'], backend=app.config['CELERY_RESULT_BACKEND'])
celery.conf.update(app.config)

# S3 client
s3 = boto3.client('s3',
    aws_access_key_id=os.environ.get('AWS_ACCESS_KEY_ID'),
    aws_secret_access_key=os.environ.get('AWS_SECRET_ACCESS_KEY')
)

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

@app.route('/upload', methods=['POST'])
@auth.login_required
@limiter.limit("10 per minute")
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'})
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'})
    if file:
        filename = secure_filename(file.filename)
        try:
            s3.upload_fileobj(file, app.config['S3_BUCKET_INPUT'], filename)
            # Enqueue processing task
            task = celery.send_task('worker.process_volume', args=[filename])
            return jsonify({'success': True, 'message': 'File uploaded and queued for processing', 'task_id': task.id})
        except ClientError as e:
            return jsonify({'error': f'S3 upload error: {str(e)}'})

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
    try:
        input_files = s3.list_objects_v2(Bucket=app.config['S3_BUCKET_INPUT'])
        output_files = s3.list_objects_v2(Bucket=app.config['S3_BUCKET_OUTPUT'])
        
        input_files = [obj['Key'] for obj in input_files.get('Contents', [])]
        output_files = [obj['Key'] for obj in output_files.get('Contents', [])]
        
        return render_template('files.html', input_files=input_files, output_files=output_files)
    except ClientError as e:
        return jsonify({'error': f'S3 list error: {str(e)}'})

@app.route('/download/<filename>')
@auth.login_required
@limiter.limit("10 per minute")
def download_file(filename):
    try:
        file = s3.get_object(Bucket=app.config['S3_BUCKET_OUTPUT'], Key=filename)
        return send_file(
            file['Body'],
            as_attachment=True,
            attachment_filename=filename
        )
    except ClientError as e:
        if e.response['Error']['Code'] == "NoSuchKey":
            abort(404)
        else:
            return jsonify({'error': f'S3 download error: {str(e)}'})

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)