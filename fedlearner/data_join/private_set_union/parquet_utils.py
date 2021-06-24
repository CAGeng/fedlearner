import os

import pyarrow as pa
import pyarrow.parquet as pq
from tensorflow import gfile

from fedlearner.data_join.private_set_union.utils import E2, E4


def encode_e2_file_path(output_path: str, file_id: [int, str]):
    return os.path.join(str(output_path), E2, str(file_id) + '.parquet')


def encode_e4_file_path(output_path: str, file_id: [int, str]):
    return os.path.join(str(output_path), E4, str(file_id) + '.parquet')


def make_dirs_if_not_exists(file_path: str):
    if not gfile.Exists(os.path.dirname(file_path)):
        gfile.MakeDirs(os.path.dirname(file_path))


def clear_file_if_exists(file_path: str):
    if gfile.Exists(os.path.dirname(file_path)):
        gfile.DeleteRecursively(file_path)


def make_or_update_dumper(dumper: [pq.ParquetWriter, None],
                          file_path: str,
                          schema: pa.Schema,
                          flavor: str = None) -> pq.ParquetWriter:
    if not dumper or dumper.where != file_path:
        if dumper:
            dumper.close()
            clear_file_if_exists(file_path)
        make_dirs_if_not_exists(file_path)
        dumper = pq.ParquetWriter(file_path, schema, flavor=flavor)
    return dumper
