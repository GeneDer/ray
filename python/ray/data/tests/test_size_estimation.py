import itertools
import os
import uuid
from typing import Iterable

import pytest

import ray
from ray.data._internal.arrow_block import ArrowBlockBuilder
from ray.data._internal.execution.interfaces.ref_bundle import RefBundle
from ray.tests.conftest import *  # noqa

SMALL_VALUE = "a" * 100
LARGE_VALUE = "a" * 10000
ARROW_SMALL_VALUE = {"value": "a" * 100}
ARROW_LARGE_VALUE = {"value": "a" * 10000}


def assert_close(actual, expected, tolerance=0.3):
    print("assert_close", actual, expected)
    assert abs(actual - expected) / expected < tolerance, (actual, expected)


def test_arrow_size(ray_start_regular_shared):
    b = ArrowBlockBuilder()
    assert b.get_estimated_memory_usage() == 0
    b.add(ARROW_SMALL_VALUE)
    assert_close(b.get_estimated_memory_usage(), 118)
    b.add(ARROW_SMALL_VALUE)
    assert_close(b.get_estimated_memory_usage(), 236)
    for _ in range(8):
        b.add(ARROW_SMALL_VALUE)
    assert_close(b.get_estimated_memory_usage(), 1180)
    for _ in range(90):
        b.add(ARROW_SMALL_VALUE)
    assert_close(b.get_estimated_memory_usage(), 11800)
    for _ in range(900):
        b.add(ARROW_SMALL_VALUE)
    assert_close(b.get_estimated_memory_usage(), 118000)
    assert b.build().num_rows == 1000


def test_arrow_size_diff_values(ray_start_regular_shared):
    b = ArrowBlockBuilder()
    assert b.get_estimated_memory_usage() == 0
    b.add(ARROW_LARGE_VALUE)
    assert b._num_compactions == 0
    assert_close(b.get_estimated_memory_usage(), 10019)
    b.add(ARROW_LARGE_VALUE)
    assert b._num_compactions == 0
    assert_close(b.get_estimated_memory_usage(), 20038)
    for _ in range(10):
        b.add(ARROW_SMALL_VALUE)
    assert_close(b.get_estimated_memory_usage(), 25178)
    for _ in range(100):
        b.add(ARROW_SMALL_VALUE)
    assert b._num_compactions == 0
    assert_close(b.get_estimated_memory_usage(), 35394)
    for _ in range(13000):
        b.add(ARROW_LARGE_VALUE)
    assert_close(b.get_estimated_memory_usage(), 130131680)
    assert b._num_compactions == 0
    for _ in range(4000):
        b.add(ARROW_LARGE_VALUE)
    assert_close(b.get_estimated_memory_usage(), 170129189)
    assert b._num_compactions == 1
    assert b.build().num_rows == 17112


def test_arrow_size_add_block(ray_start_regular_shared):
    b = ArrowBlockBuilder()
    for _ in range(2000):
        b.add(ARROW_LARGE_VALUE)
    block = b.build()
    b2 = ArrowBlockBuilder()
    for _ in range(5):
        b2.add_block(block)
    assert b2._num_compactions == 0
    assert_close(b2.get_estimated_memory_usage(), 100040020)
    assert b2.build().num_rows == 10000


def test_split_read_csv(ray_start_regular_shared, tmp_path):
    ctx = ray.data.context.DataContext.get_current()

    def gen(name):
        path = os.path.join(tmp_path, name)
        ray.data.range(1000, override_num_blocks=1).map(
            lambda _: {"out": LARGE_VALUE}
        ).write_csv(path)
        return ray.data.read_csv(path, override_num_blocks=1)

    # 20MiB
    ctx.target_max_block_size = 20_000_000
    ds1 = gen("out1")
    assert ds1._block_num_rows() == [1000]

    # 3MiB
    ctx.target_max_block_size = 3_000_000
    ds2 = gen("out2")
    nrow = ds2._block_num_rows()
    assert 3 < len(nrow) < 5, nrow
    for x in nrow[:-1]:
        assert 200 < x < 400, (x, nrow)

    # 1MiB
    ctx.target_max_block_size = 1_000_000
    ds3 = gen("out3")
    nrow = ds3._block_num_rows()
    assert 8 < len(nrow) < 12, nrow
    for x in nrow[:-1]:
        assert 80 < x < 120, (x, nrow)

    # Disabled.
    # Setting a huge block size effectively disables block splitting.
    ctx.target_max_block_size = 2**64
    ds4 = gen("out4")
    assert ds4._block_num_rows() == [1000]


def test_split_read_parquet(ray_start_regular_shared, tmp_path):
    ctx = ray.data.context.DataContext.get_current()

    def gen(name):
        path = os.path.join(tmp_path, name)
        ds = (
            ray.data.range(200000, override_num_blocks=1)
            .map(lambda _: {"out": uuid.uuid4().hex})
            .materialize()
        )
        # Fully execute the operations prior to write, because with
        # override_num_blocks=1, there is only one task; so the write operator
        # will only write to one file, even though there are multiple
        # blocks created by block splitting.
        ds.write_parquet(path)
        return ray.data.read_parquet(path, override_num_blocks=1)

    # 20MiB
    ctx.target_max_block_size = 20_000_000
    ds1 = gen("out1")
    assert ds1._block_num_rows() == [200000]

    # 3MiB
    ctx.target_max_block_size = 3_000_000
    ds2 = gen("out2")
    nrow = ds2._block_num_rows()
    assert 2 < len(nrow) < 5, nrow
    for x in nrow[:-1]:
        assert 50000 < x < 95000, (x, nrow)

    # 1MiB
    ctx.target_max_block_size = 1_000_000
    ds3 = gen("out3")
    nrow = ds3._block_num_rows()
    assert 6 < len(nrow) < 12, nrow
    for x in nrow[:-1]:
        assert 20000 < x < 35000, (x, nrow)


@pytest.mark.parametrize("use_actors", [False, True])
def test_split_map(shutdown_only, use_actors):
    ray.shutdown()
    ray.init(num_cpus=3)
    kwargs = {}

    def arrow_udf(x):
        return ARROW_LARGE_VALUE

    def identity_udf(x):
        return x

    class ArrowUDFClass:
        def __call__(self, x):
            return ARROW_LARGE_VALUE

    class IdentityUDFClass:
        def __call__(self, x):
            return x

    if use_actors:
        kwargs = {"compute": ray.data.ActorPoolStrategy()}
        arrow_fn = ArrowUDFClass
        identity_fn = IdentityUDFClass
    else:
        arrow_fn = arrow_udf
        identity_fn = identity_udf

    # Arrow block
    ctx = ray.data.context.DataContext.get_current()
    ctx.target_max_block_size = 20_000_000

    ds2 = ray.data.range(1000, override_num_blocks=1).map(arrow_fn, **kwargs)
    bundles = ds2.map(identity_fn, **kwargs).iter_internal_ref_bundles()

    blocks = _fetch_blocks(bundles)
    num_rows = _get_total_rows(blocks)

    assert len(blocks) == 1
    assert num_rows == 1000

    ctx.target_max_block_size = 2_000_000
    ds3 = ray.data.range(1000, override_num_blocks=1).map(arrow_fn, **kwargs)
    bundles = ds3.map(identity_fn, **kwargs).iter_internal_ref_bundles()

    blocks = _fetch_blocks(bundles)
    num_rows = _get_total_rows(blocks)

    assert 4 < len(blocks) < 7
    assert num_rows == 1000

    # Disabled.
    # Setting a huge block size effectively disables block splitting.
    ctx.target_max_block_size = 2**64

    ds3 = ray.data.range(1000, override_num_blocks=1).map(arrow_fn, **kwargs)
    bundles = ds3.map(identity_fn, **kwargs).iter_internal_ref_bundles()

    blocks = _fetch_blocks(bundles)
    num_rows = _get_total_rows(blocks)

    assert len(blocks) == 1
    assert num_rows == 1000


def _get_total_rows(blocks):
    return sum([b.num_rows for b in blocks])


def _fetch_blocks(bundles: Iterable[RefBundle]):
    return ray.get(list(itertools.chain(*[b.block_refs for b in bundles])))


def test_split_flat_map(ray_start_regular_shared):
    ctx = ray.data.context.DataContext.get_current()
    # Arrow block
    ctx.target_max_block_size = 20_000_000

    ds2 = ray.data.range(1000, override_num_blocks=1).map(lambda _: ARROW_LARGE_VALUE)
    bundles = ds2.flat_map(lambda x: [x]).iter_internal_ref_bundles()

    blocks = _fetch_blocks(bundles)
    num_rows = _get_total_rows(blocks)

    assert len(blocks) == 1
    assert num_rows == 1000

    ctx.target_max_block_size = 2_000_000
    ds3 = ray.data.range(1000, override_num_blocks=1).map(lambda _: ARROW_LARGE_VALUE)
    bundles = ds3.flat_map(lambda x: [x]).iter_internal_ref_bundles()

    blocks = _fetch_blocks(bundles)
    num_rows = _get_total_rows(blocks)

    assert 4 < len(blocks) < 7
    assert num_rows == 1000


def test_split_map_batches(ray_start_regular_shared):
    ctx = ray.data.context.DataContext.get_current()
    # Arrow block
    ctx.target_max_block_size = 20_000_000

    ds2 = ray.data.range(1000, override_num_blocks=1).map(lambda _: ARROW_LARGE_VALUE)
    bundles = ds2.map_batches(lambda x: x, batch_size=1).iter_internal_ref_bundles()

    blocks = _fetch_blocks(bundles)
    num_rows = _get_total_rows(blocks)

    assert len(blocks) == 1
    assert num_rows == 1000

    ctx.target_max_block_size = 2_000_000
    ds3 = ray.data.range(1000, override_num_blocks=1).map(lambda _: ARROW_LARGE_VALUE)
    bundles = ds3.map_batches(lambda x: x, batch_size=16).iter_internal_ref_bundles()

    blocks = _fetch_blocks(bundles)
    num_rows = _get_total_rows(blocks)

    assert 4 < len(blocks) < 7
    assert num_rows == 1000


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main(["-v", __file__]))
