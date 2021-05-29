"""

    The CaseOnDisk class objectifies an FMU case (results) as it appears on the disk.

"""

import os
import glob
import time
import logging
import warnings

import yaml
import pandas as pd

from fmu.sumo.uploader._fileondisk import FileOnDisk
from fmu.sumo.uploader._upload_files import upload_files

logger = logging.getLogger(__name__)
logger.setLevel(logging.CRITICAL)

# pylint: disable=C0103 # allow non-snake case variable names


class CaseOnDisk:
    """
    Class to hold information about an ERT run on disk.

    The CaseOnDisk object is a representation of files belonging to an FMU case,
    as they are stored on the Scratch disk.

    A Case in this context is a set of metadata describing this particular case,
    and an arbitrary number of files belonging to this case. Each file is in reality
    a file pair, consisting of a data file (could be any file type) and a metadata file
    (yaml formatted, according) to FMU standards.

    Example for initialization:
        >>> from fmu import sumo

        >>> env = 'dev'
        >>> case_metadata_path = 'path/to/case_metadata.yaml'
        >>> search_path = 'path/to/search_path/'

        >>> sumo_connection = sumo.SumoConnection(env=env)
        >>> e = sumo.CaseOnDisk(
                case_metadata_path=case_metadata_path,
                sumo_connection=sumo_connection)

        After initialization, files must be explicitly indexed into the CaseOnDisk object:

        >>> e.add_files(search_path)

        When initialized, the case can be uploaded to Sumo:

        >>> e.upload()

    Args:
        case_metadata_path (str): Path to the case_metadata file for the case
        sumo_connection (fmu.sumo.SumoConnection): SumoConnection object


    """

    def __init__(self, case_metadata_path: str, sumo_connection, verbosity="DEBUG"):
        """
        case_metadata_path (str): Path to case_metadata for case
        api (SumoConnection instance): Connection to Sumo.
        """

        logger.setLevel(level=verbosity)

        self.sumo_connection = sumo_connection        

        self.case_metadata = _load_case_metadata(case_metadata_path)
        self._fmu_case_uuid = self._get_fmu_case_uuid()
        logger.debug("self._fmu_case_uuid is %s", self._fmu_case_uuid)
        self._sumo_parent_id = self._get_sumo_parent_id()
        logger.debug("self._sumo_parent_id is %s", self._sumo_parent_id)
        self._files = []

    def __str__(self):
        s = f"{self.__class__}, {len(self._files)} files."

        if self._sumo_parent_id is not None:
            s += f"\nInitialized on Sumo. Sumo_ID: {self._sumo_parent_id}"
        else:
            s += "\nNot initialized on Sumo."

        return s

    def __repr__(self):
        return str(self.__str__)

    @property
    def sumo_parent_id(self):
        """Return the sumo parent ID"""
        return self._sumo_parent_id

    @property
    def fmu_case_uuid(self):
        """Return the fmu_case_uuid"""
        return self._fmu_case_uuid

    @property
    def files(self):
        """Return the files"""
        return self._files

    def add_files(self, search_string):
        """Add files to the case, based on search string"""
        file_paths = _find_file_paths(search_string)

        for file_path in file_paths:
            try:
                file = FileOnDisk(path=file_path)
                self._files.append(file)

            except IOError as err:
                info = f"{err}. No metadata, skipping file."
                warnings.warn(info)
                print(info)

    def _get_sumo_parent_id(self):
        """Call sumo, check if the case is already there. Use fmu_case_uuid for this."""
        query = f"fmu_case_uuid:{self.fmu_case_uuid}"
        search_results = self.sumo_connection.api.searchroot(query, search_size=2)

        # To catch crazy rare situation when index is empty (first upload to new index)
        if not search_results.get("hits"):
            return None

        hits = search_results.get("hits").get("hits")

        if len(hits) == 0:
            return None

        if len(hits) == 1:
            sumo_parent_id = hits[0].get("_id")
            return sumo_parent_id

        raise ValueError(
            f"More than one hit for fmu.case.uuid {self.fmu_case_uuid} found on Sumo"
        )

    def register(self):
        """
        Register this case on Sumo.
        Assumptions: If registering an already existing case, it will be overwritten.
        ("register" might be a bad word for this...)

        Returns:
            sumo_parent_id (uuid4): Unique ID for this case on Sumo
        """
        logger.info("Registering case on Sumo")

        sumo_parent_id = self._upload_case_metadata(self.case_metadata)
        self._sumo_parent_id = sumo_parent_id

        logger.info("Case registered. SumoID: {}".format(sumo_parent_id))

        return sumo_parent_id

    def _upload_case_metadata(self, case_metadata: dict):
        """Given a case_metadata dict, upload it to Sumo"""
        response = self.sumo_connection.api.save_top_level_json(json=case_metadata)

        returned_object_id = response.json().get("objectid")

        return returned_object_id

    def _get_fmu_case_uuid(self):
        """Look up and return case_id from case_metadata"""
        fmu_case_uuid = self.case_metadata.get("fmu").get("case").get("uuid")

        if not fmu_case_uuid:
            raise ValueError("Could not get fmu_case_uuid from case metadata")

        return fmu_case_uuid

    def upload(self, threads=4, max_attempts=3, register_case=False):
        """
        Trigger upload of files in this case.

            Get sumo_parent_id. If None, case is not registered on Sumo.

            Upload all indexed files. Collect the files that have been uploaded OK, the
            ones that have failed and the ones that have been rejected.

            Retry the failed uploads X times.

        """

        if self.sumo_parent_id is None:
            logger.info("Case is not registered on Sumo")

            if register_case:
                self.register()
            else:
                raise IOError(
                    "Case is not registered on sumo. "
                    "Set register_case to True if you want to do so."
                )

        if not self.files:
            raise FileExistsError("No files to upload. Check search string.")

        ok_uploads = []
        failed_uploads = []
        rejected_uploads = []
        files_to_upload = [f for f in self.files]

        attempts = 0
        _t0 = time.perf_counter()

        while files_to_upload and attempts < max_attempts:
            upload_results = upload_files(
                files=files_to_upload,
                sumo_parent_id=self.sumo_parent_id,
                sumo_connection=self.sumo_connection,
                threads=threads,
            )

            ok_uploads += upload_results.get("ok_uploads")
            rejected_uploads += upload_results.get("rejected_uploads")
            failed_uploads = upload_results.get("failed_uploads")

            if not failed_uploads:
                break

            files_to_upload = [f.get("file") for f in failed_uploads]

            attempts += 1

            time.sleep(3)
            logger.debug(
                "Retrying {} failed uploads after waiting 3 seconds".format(
                    len(failed_uploads)
                )
            )

        if files_to_upload:
            warnings.warn("Stopping after {} attempts".format(attempts))

        _dt = time.perf_counter() - _t0

        if len(ok_uploads) > 0:
            upload_statistics = _calculate_upload_stats(ok_uploads)
            logger.info(upload_statistics)

        if failed_uploads:
            logger.info(f"{len(failed_uploads)} files failed to be uploaded")

            for u in failed_uploads[0:4]:
                logger.info("\n" + "=" * 50)

                logger.info(f"Filepath: {u.get('blob_file_path')}")
                logger.info(
                    f"Metadata: [{u.get('metadata_upload_response_status_code')}] "
                    f"{u.get('metadata_upload_response_text')}"
                )
                logger.info(
                    f"Blob: [{u.get('blob_upload_response_status_code')}] "
                    f"{u.get('blob_upload_response_status_text')}"
                )

        if rejected_uploads:
            logger.info(
                f"\n\n{len(rejected_uploads)} files rejected by Sumo. First 5 rejected files:"
            )

            for u in rejected_uploads[0:4]:
                logger.info("\n" + "=" * 50)

                logger.info(f"Filepath: {u.get('blob_file_path')}")
                logger.info(
                    f"Metadata: [{u.get('metadata_upload_response_status_code')}] "
                    f"{u.get('metadata_upload_response_text')}"
                )
                logger.info(
                    f"Blob: [{u.get('blob_upload_response_status_code')}] "
                    f"{u.get('blob_upload_response_status_text')}"
                )

        if failed_uploads:
            logger.info(
                f"\n\n{len(failed_uploads)} files rejected by Sumo. First 5 rejected files:"
            )

            for u in failed_uploads[0:4]:
                logger.info("\n" + "=" * 50)

                logger.info(f"Filepath: {u.get('blob_file_path')}")
                logger.info(
                    f"Metadata: [{u.get('metadata_upload_response_status_code')}] "
                    f"{u.get('metadata_upload_response_text')}"
                )
                logger.info(
                    f"Blob: [{u.get('blob_upload_response_status_code')}] "
                    f"{u.get('blob_upload_response_status_text')}"
                )

        print(
            f"Total: {len(self.files)}"
            f"\nOK: {len(ok_uploads)}"
            f"\nFailures: {len(failed_uploads)}"
            f"\nRejected: {len(rejected_uploads)}"
            f"\nWall time: {_dt} seconds"
        )

        return ok_uploads


def _load_case_metadata(case_metadata_path: str):
    """Given case_metadata path, load the yaml file from disk, return dict"""

    if not os.path.isfile(case_metadata_path):
        raise IOError(f"case metadata not found: {case_metadata_path}")

    with open(case_metadata_path, "r") as stream:
        yaml_data = yaml.safe_load(stream)

    return yaml_data


def _find_file_paths(search_string):
    """Given a search string, return yielded valid files as list of FileOnDisk instances"""
    files = [f for f in glob.glob(search_string) if os.path.isfile(f)]

    if len(files) == 0:
        info = "No files found! Please, check the search string."
        warnings.warn(info)
        print(info)

        info = f"Search string: {search_string}"
        warnings.warn(info)
        print(info)

    return files


def _calculate_upload_stats(uploads):
    """
    Given a list of results from file upload, calculate and return timing statistics for uploads
    """

    df = pd.DataFrame().from_dict(uploads)

    stats = {
        "blob": {
            "upload_time": {
                "mean": df["blob_upload_time_elapsed"].mean(),
                "max": df["blob_upload_time_elapsed"].max(),
                "min": df["blob_upload_time_elapsed"].min(),
                "std": df["blob_upload_time_elapsed"].std(),
            },
        },
        "metadata": {
            "upload_time": {
                "mean": df["metadata_upload_time_elapsed"].mean(),
                "max": df["metadata_upload_time_elapsed"].max(),
                "min": df["metadata_upload_time_elapsed"].min(),
                "std": df["metadata_upload_time_elapsed"].std(),
            },
        },
    }

    return stats
