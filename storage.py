"""
Thin wrappers around object-storage I/O.
Keep these functions as stubs for any backend that is not yet configured so
that the rest of the pipeline can be unit-tested by mocking this module.
"""
import httpx
import globals


def fetch_from_storage(url: str, timeout: float = 120.0) -> bytes:
    """
    Downloads raw bytes from a pre-signed or public storage URL.
    Raises httpx.HTTPStatusError on 4xx / 5xx responses.
    """
    with httpx.Client(timeout=timeout) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.content


def store_to_storage(file_bytes: bytes, path: str) -> str:
    """
    Uploads bytes to the configured object-storage backend and returns the
    URL that can later be passed to fetch_from_storage.

    TODO: implement for your storage backend (GCS, S3, Azure Blob, etc.)
          Credentials should be read from globals.secret_data.

    Example for GCS:
        from google.cloud import storage as gcs
        client = gcs.Client()
        bucket = client.bucket(globals.secret_data['GCS_BUCKET'])
        blob = bucket.blob(path)
        blob.upload_from_string(file_bytes)
        return blob.public_url
    """
    raise NotImplementedError(
        "store_to_storage is not yet implemented. "
        "Add your storage backend (GCS / S3 / Azure) here and read "
        "credentials from globals.secret_data."
    )
