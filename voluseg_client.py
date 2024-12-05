import os
import requests
import time
from urllib.parse import urljoin
import boto3

class VolusegClient:
    def __init__(self, base_url, username, password):
        """Initialize the Voluseg client.
        
        Args:
            base_url: Base URL of the Voluseg server
            username: Username for authentication
            password: Password for authentication
        """
        self.base_url = base_url.rstrip('/')
        self.auth = (username, password)
        self.session = requests.Session()
        self.session.auth = self.auth

    def _make_request(self, method, endpoint, **kwargs):
        """Make an HTTP request to the API."""
        url = urljoin(self.base_url, endpoint)
        response = self.session.request(method, url, **kwargs)
        response.raise_for_status()
        return response.json()

    def process_file(self, file_path, parameters=None, polling_interval=30):
        """Process a file through Voluseg.
        
        Args:
            file_path: Path to the input file
            parameters: Optional processing parameters
            polling_interval: Seconds between status checks
            
        Returns:
            Downloaded output file path
        """
        # Get file info
        file_name = os.path.basename(file_path)
        file_size = os.path.getsize(file_path)
        
        # Initialize upload
        upload_info = self._make_request('POST', '/api/upload', json={
            'file_name': file_name,
            'file_size': file_size,
            'parameters': parameters
        })
        
        # Upload file parts
        parts = []
        with open(file_path, 'rb') as f:
            for i, url in enumerate(upload_info['urls'], 1):
                # Calculate part data
                part_data = f.read(upload_info['part_size'])
                if not part_data:
                    break
                    
                # Upload part
                response = requests.put(url, data=part_data)
                parts.append({
                    'PartNumber': i,
                    'ETag': response.headers['ETag']
                })
        
        # Complete upload and start processing
        completion = self._make_request('POST', '/api/complete_upload', json={
            'file_name': upload_info['file_name'],
            'upload_id': upload_info['upload_id'],
            'parts': parts,
            'parameters': parameters
        })
        
        task_id = completion['task_id']
        
        # Poll for completion
        while True:
            status = self._make_request('GET', f'/api/status/{task_id}')
            
            if status.get('status') == 'failed':
                raise Exception(f"Processing failed: {status.get('error')}")
                
            if status.get('status') == 'completed':
                # Download result
                output_path = os.path.splitext(file_path)[0] + '_output.zip'
                response = requests.get(status['download_url'])
                with open(output_path, 'wb') as f:
                    f.write(response.content)
                return output_path
                
            time.sleep(polling_interval) 