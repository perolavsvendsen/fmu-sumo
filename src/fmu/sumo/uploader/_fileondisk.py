import yaml
import os
import datetime
import time
import logging

from sumo.wrapper._request_error import AuthenticationError, TransientError, PermanentError


def path_to_yaml_path(path):
    """
    Given a path, return the corresponding yaml file path
    according to FMU standards.
    /my/path/file.txt --> /my/path/.file.txt.yaml
    """

    dir_name = os.path.dirname(path)
    basename = os.path.basename(path)

    return os.path.join(dir_name, f'.{basename}.yaml')


def parse_yaml(path):
    if not os.path.isfile(path):
        raise IOError(f'File does not exist: {path}')

    with open(path, 'r') as stream:
        data = yaml.safe_load(stream)

    return data


def file_to_byte_string(path):
    """
    Given an path to a file, read as bytes, return byte string.
    """

    with open(path, 'rb') as f:
        byte_string = f.read()

    return byte_string


def _datetime_now():
    """Return datetime now on FMU standard format"""
    return datetime.datetime.now().isoformat()


class FileOnDisk:
    def __init__(self, path: str, metadata_path=None):
        """
        path (str): Path to file
        metadata_path (str): Path to metadata file. If not provided, 
                             path will be derived from file path.
        """
        self.metadata_path = metadata_path if metadata_path else path_to_yaml_path(path)
        self.path = os.path.abspath(path)
        self._metadata = parse_yaml(self.metadata_path)
        self._byte_string = file_to_byte_string(path)

        self._size = None
        self._d_type = None
        self._case_name = None
        self._basename = os.path.basename(self.path)
        self._dir_name = os.path.dirname(self.path)
        self._file_format = None

        self._sumo_child_id = None
        self._sumo_parent_id = None
        self._sumo_blob_id = None

    def __repr__(self):
        if not self.metadata:
            return f'\n# {self.__class__} \n# No metadata'

        s = f'\n# {self.__class__}'
        s += f'\n# Disk path: {self.path}'
        s += f'\n# Basename: {self.basename}'
        s += f'\n# Byte string length: {len(self.byte_string)}'
 
        if self.sumo_child_id is None:
            s += '\n# Not uploaded to Sumo'
        else:
            s += f'\n# Uploaded to Sumo. Sumo_ID: {self.sumo_child_id}'

        return s

    @property
    def sumo_parent_id(self):
        return self._sumo_parent_id

    @property
    def sumo_child_id(self):
        return self._sumo_child_id

    @property
    def sumo_blob_id(self):
        return self._sumo_blob_id

    @property
    def size(self):
        """Size of the file"""
        if self._size is None:
            self._size = os.path.getsize(self.path)

        return self._size

    @property
    def basename(self):
        return self._basename

    @property
    def dir_name(self):
        return self._dir_name

    @property
    def metadata(self):
        return self._metadata

    @property
    def byte_string(self):
        return self._byte_string

    def _upload_metadata(self, sumo_connection, sumo_parent_id):
        response = sumo_connection.api.save_child_level_json(json=self.metadata, parent_id=sumo_parent_id)
        return response

    def _upload_byte_string(self, sumo_connection, object_id, blob_url):
        response = sumo_connection.api.save_blob(blob=self.byte_string, object_id=object_id, url=blob_url)
        return response

    def upload_to_sumo(self, sumo_parent_id, sumo_connection):
        """Upload this file to Sumo"""

        if not sumo_parent_id:
            raise ValueError(f'Upload failed, sumo_parent_id passed to upload_to_sumo: {sumo_parent_id}')

        _t0 = time.perf_counter()
        _t0_metadata = time.perf_counter()

        result = {}

        try:

            # We need these included even if returning before blob upload
            result['blob_file_path'] = self.path
            result['blob_file_size'] = self.size

            response = self._upload_metadata(sumo_connection=sumo_connection, sumo_parent_id=sumo_parent_id)

            _t1_metadata = time.perf_counter()

            result['metadata_upload_response_status_code'] = response.status_code
            result['metadata_upload_response_text'] = response.text
            result['metadata_upload_time_start'] = _t0_metadata
            result['metadata_upload_time_end'] = _t1_metadata
            result['metadata_upload_time_elapsed'] = _t1_metadata-_t0_metadata
            result['metadata_file_path'] = self.metadata_path
            result['metadata_file_size'] = self.size

        except TransientError as err:
            result['status'] = 'failed'
            result['metadata_upload_response_status_code'] = err.code
            result['metadata_upload_response_text'] = err.message
            return result
        except AuthenticationError as err:
            result['status'] = 'rejected'
            result['metadata_upload_response_status_code'] = err.code
            result['metadata_upload_response_text'] = err.message
            return result
        except PermanentError as err:
            result['status'] = 'rejected'
            result['metadata_upload_response_status_code'] = err.code
            result['metadata_upload_response_text'] = err.message
            return result

        self._sumo_parent_id = sumo_parent_id
        self._sumo_child_id = response.json().get('objectid')

        blob_url = response.json().get('blob_url')

        # UPLOAD BLOB
        _t0_blob = time.perf_counter()

        try:
            response = self._upload_byte_string(sumo_connection=sumo_connection,
                                                object_id=self._sumo_child_id, blob_url=blob_url)
        except OSError as err:
            logging.info(f'Upload failed: {err}')
            result['status'] = 'failed'
            return result

        _t1_blob = time.perf_counter()

        result['blob_upload_response_status_code'] = response.status_code
        result['blob_upload_response_text'] = response.text
        result['blob_upload_time_start'] = _t0_blob
        result['blob_upload_time_end'] = _t1_blob
        result['blob_upload_time_elapsed'] = _t1_blob-_t0_blob

        if response.status_code not in [200, 201]:
            logging.info(f'Upload failed: {response}')
            result['status'] = 'failed'
        else:
            result['status'] = 'ok'

        return result
