import asyncio
import gzip
import json
import logging
import pickle
import shutil
import threading
from typing import Awaitable, Callable, Any, Coroutine, Optional, IO

import aiohttp
import cbor2
import h5pyd
import numpy as np

import pytest
import zmq.asyncio
import zmq
from pydantic_core import Url

from dranspose.event import EventData
from dranspose.ingester import Ingester
from dranspose.ingesters.zmqpull_single import (
    ZmqPullSingleIngester,
    ZmqPullSingleSettings,
)
from dranspose.protocol import (
    StreamName,
    WorkerName,
    VirtualWorker,
    VirtualConstraint,
)

from dranspose.replay import replay
from dranspose.worker import Worker
from tests.utils import wait_for_controller, wait_for_finish


async def dump_data(
    reducer: Callable[[Optional[str]], Awaitable[None]],
    create_worker: Callable[[WorkerName], Awaitable[Worker]],
    create_ingester: Callable[[Ingester], Awaitable[Ingester]],
    stream_eiger: Callable[[zmq.Context[Any], int, int], Coroutine[Any, Any, None]],
    stream_orca: Callable[[zmq.Context[Any], int, int], Coroutine[Any, Any, None]],
    stream_small: Callable[[zmq.Context[Any], int, int], Coroutine[Any, Any, None]],
    tmp_path: Any,
):
    await reducer(None)
    await create_worker(WorkerName("w1"))
    await create_worker(WorkerName("w2"))
    await create_worker(WorkerName("w3"))

    p_eiger = tmp_path / "eiger_dump.cbors"
    print(p_eiger, type(p_eiger))

    await create_ingester(
        ZmqPullSingleIngester(
            settings=ZmqPullSingleSettings(
                ingester_streams=[StreamName("eiger")],
                upstream_url=Url("tcp://localhost:9999"),
                dump_path=p_eiger,
            ),
        )
    )
    await create_ingester(
        ZmqPullSingleIngester(
            settings=ZmqPullSingleSettings(
                ingester_streams=[StreamName("orca")],
                upstream_url=Url("tcp://localhost:9998"),
                ingester_url=Url("tcp://localhost:10011"),
            ),
        )
    )
    await create_ingester(
        ZmqPullSingleIngester(
            settings=ZmqPullSingleSettings(
                ingester_streams=[StreamName("alba")],
                upstream_url=Url("tcp://localhost:9997"),
                ingester_url=Url("tcp://localhost:10012"),
            ),
        )
    )
    await create_ingester(
        ZmqPullSingleIngester(
            settings=ZmqPullSingleSettings(
                ingester_streams=[StreamName("slow")],
                upstream_url=Url("tcp://localhost:9996"),
                ingester_url=Url("tcp://localhost:10013"),
            ),
        )
    )

    await wait_for_controller(
        streams={
            StreamName("eiger"),
            StreamName("orca"),
            StreamName("alba"),
            StreamName("slow"),
        }
    )
    async with aiohttp.ClientSession() as session:
        p_prefix = tmp_path / "dump_"
        logging.info("prefix is %s encoded: %s", p_prefix, str(p_prefix).encode("utf8"))

        resp = await session.post(
            "http://localhost:5000/api/v1/parameter/dump_prefix",
            data=str(p_prefix).encode("utf8"),
        )
        assert resp.status == 200

        ntrig = 10
        resp = await session.post(
            "http://localhost:5000/api/v1/mapping",
            json={
                "eiger": [
                    [
                        VirtualWorker(constraint=VirtualConstraint(2 * i)).model_dump(
                            mode="json"
                        )
                    ]
                    for i in range(1, ntrig)
                ],
                "orca": [
                    [
                        VirtualWorker(
                            constraint=VirtualConstraint(2 * i + 1)
                        ).model_dump(mode="json")
                    ]
                    for i in range(1, ntrig)
                ],
                "alba": [
                    [
                        VirtualWorker(constraint=VirtualConstraint(2 * i)).model_dump(
                            mode="json"
                        ),
                        VirtualWorker(
                            constraint=VirtualConstraint(2 * i + 1)
                        ).model_dump(mode="json"),
                    ]
                    for i in range(1, ntrig)
                ],
                "slow": [
                    [
                        VirtualWorker(constraint=VirtualConstraint(2 * i)).model_dump(
                            mode="json"
                        ),
                        VirtualWorker(
                            constraint=VirtualConstraint(2 * i + 1)
                        ).model_dump(mode="json"),
                    ]
                    if i % 4 == 0
                    else None
                    for i in range(1, ntrig)
                ],
            },
        )
        assert resp.status == 200
        uuid = await resp.json()

    context = zmq.asyncio.Context()

    asyncio.create_task(stream_eiger(context, 9999, ntrig - 1))
    asyncio.create_task(stream_orca(context, 9998, ntrig - 1))
    asyncio.create_task(stream_small(context, 9997, ntrig - 1))
    asyncio.create_task(stream_small(context, 9996, ntrig // 4))

    await wait_for_finish()

    context.destroy()

    return p_eiger, p_prefix, uuid


def generate_params(tmp_path):
    par_file = tmp_path / "parameters.json"

    with open(par_file, "w") as f:
        json.dump([{"name": "roi1", "data": "[10,20]"}], f)
    return par_file


@pytest.mark.skipif("config.getoption('rust')", reason="rust does not support dumping")
@pytest.mark.asyncio
async def test_replay(
    controller: None,
    reducer: Callable[[Optional[str]], Awaitable[None]],
    create_worker: Callable[[WorkerName], Awaitable[Worker]],
    create_ingester: Callable[[Ingester], Awaitable[Ingester]],
    stream_eiger: Callable[[zmq.Context[Any], int, int], Coroutine[Any, Any, None]],
    stream_orca: Callable[[zmq.Context[Any], int, int], Coroutine[Any, Any, None]],
    stream_small: Callable[[zmq.Context[Any], int, int], Coroutine[Any, Any, None]],
    tmp_path: Any,
) -> None:
    p_eiger, p_prefix, uuid = await dump_data(
        reducer,
        create_worker,
        create_ingester,
        stream_eiger,
        stream_orca,
        stream_small,
        tmp_path,
    )
    # read dump

    par_file = generate_params(tmp_path)

    replay(
        "examples.dummy.worker:FluorescenceWorker",
        "examples.dummy.reducer:FluorescenceReducer",
        [p_eiger, f"{p_prefix}orca-ingester-{uuid}.cbors"],
        None,
        par_file,
    )


@pytest.mark.skipif("config.getoption('rust')", reason="rust does not support dumping")
@pytest.mark.asyncio
async def test_replay_gzip(
    controller: None,
    reducer: Callable[[Optional[str]], Awaitable[None]],
    create_worker: Callable[[WorkerName], Awaitable[Worker]],
    create_ingester: Callable[[Ingester], Awaitable[Ingester]],
    stream_eiger: Callable[[zmq.Context[Any], int, int], Coroutine[Any, Any, None]],
    stream_orca: Callable[[zmq.Context[Any], int, int], Coroutine[Any, Any, None]],
    stream_small: Callable[[zmq.Context[Any], int, int], Coroutine[Any, Any, None]],
    tmp_path: Any,
) -> None:
    p_eiger, p_prefix, uuid = await dump_data(
        reducer,
        create_worker,
        create_ingester,
        stream_eiger,
        stream_orca,
        stream_small,
        tmp_path,
    )

    with open(f"{p_prefix}orca-ingester-{uuid}.cbors", "rb") as f_in:
        with gzip.open(f"{p_prefix}orca-ingester-{uuid}.cbors.gz", "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)

    stop_event = threading.Event()
    done_event = threading.Event()

    par_file = generate_params(tmp_path)

    thread = threading.Thread(
        target=replay,
        args=(
            "examples.test.worker:TestWorker",
            "examples.test.reducer:TestReducer",
            [p_eiger, f"{p_prefix}orca-ingester-{uuid}.cbors.gz"],
            None,
            par_file,
        ),
        kwargs={"port": 5010, "stop_event": stop_event, "done_event": done_event},
    )
    thread.start()

    logging.info("keep webserver alive")
    done_event.wait()
    logging.info("replay done")

    async with aiohttp.ClientSession() as session:
        ntrig = 10
        st = await session.get("http://localhost:5010/api/v1/result/")
        content = await st.content.read()
        result = pickle.loads(content)[0]
        assert list(result["results"].keys()) == list(range(ntrig + 1))
        logging.warning(
            "params in reducer is %s", result["results"][5][0].streams.keys()
        )
        ev5: EventData = result["results"][5][0]
        assert ev5.event_number == 5
        assert ev5.streams[StreamName("eiger")].typ == "STINS"
        eiger_frame_header = ev5.streams[StreamName("eiger")].frames[0]
        eiger_frame_payload = ev5.streams[StreamName("eiger")].frames[1]
        assert isinstance(eiger_frame_header, bytes)
        assert isinstance(eiger_frame_payload, bytes)
        assert eiger_frame_header.startswith(
            b'{"htype": "image", "frame": 4, "shape": [1475, 831], "type": "uint16", "compression": "none", "msg_number": 5, "timestamps": {"dummy": "'
        )
        assert len(eiger_frame_payload) > 4500
        assert ev5.streams[StreamName("orca")].typ == "STINS"
        orca_frame_header = ev5.streams[StreamName("orca")].frames[0]
        orca_frame_payload = ev5.streams[StreamName("orca")].frames[1]
        assert isinstance(orca_frame_header, bytes)
        assert isinstance(orca_frame_payload, bytes)
        assert orca_frame_header.startswith(
            b'{"htype": "image", "frame": 4, "shape": [2000, 4000], "type": "uint16", "compression": "none", "msg_number": 5}'
        )
        assert len(orca_frame_payload) > 4500
    logging.info("shut down server")
    stop_event.set()

    thread.join()


@pytest.mark.skipif("config.getoption('rust')", reason="rust does not support dumping")
@pytest.mark.asyncio
async def test_replay_pklparam(
    controller: None,
    reducer: Callable[[Optional[str]], Awaitable[None]],
    create_worker: Callable[[WorkerName], Awaitable[Worker]],
    create_ingester: Callable[[Ingester], Awaitable[Ingester]],
    stream_eiger: Callable[[zmq.Context[Any], int, int], Coroutine[Any, Any, None]],
    stream_orca: Callable[[zmq.Context[Any], int, int], Coroutine[Any, Any, None]],
    stream_small: Callable[[zmq.Context[Any], int, int], Coroutine[Any, Any, None]],
    tmp_path: Any,
) -> None:
    p_eiger, p_prefix, uuid = await dump_data(
        reducer,
        create_worker,
        create_ingester,
        stream_eiger,
        stream_orca,
        stream_small,
        tmp_path,
    )

    bin_file = tmp_path / "binparams.pkl"

    fb: IO[bytes]
    with open(bin_file, "wb") as fb:
        arr = np.ones((10, 10))
        pickle.dump(
            [
                {"name": "roi1", "data": "[10,20]"},
                {"name": "file_parameter_file", "data": pickle.dumps(arr)},
            ],
            fb,
        )

    stop_event = threading.Event()
    done_event = threading.Event()

    thread = threading.Thread(
        target=replay,
        args=(
            "examples.dummy.worker:FluorescenceWorker",
            "examples.dummy.reducer:FluorescenceReducer",
            [p_eiger, f"{p_prefix}orca-ingester-{uuid}.cbors"],
            None,
            bin_file,
        ),
        kwargs={"port": 5010, "stop_event": stop_event, "done_event": done_event},
    )
    thread.start()

    logging.info("keep webserver alive")
    done_event.wait()
    logging.info("replay done")

    def work_first() -> None:
        f = h5pyd.File("http://localhost:5010/", "r")
        logging.info("file %s", list(f.keys()))
        logging.info("map %s", list(f["map"].keys()))
        assert list(f.keys()) == ["map"]

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, work_first)

    logging.info("shut down server")
    stop_event.set()

    thread.join()


@pytest.mark.skipif("config.getoption('rust')", reason="rust does not support dumping")
@pytest.mark.asyncio
async def test_replay_cborparam(
    controller: None,
    reducer: Callable[[Optional[str]], Awaitable[None]],
    create_worker: Callable[[WorkerName], Awaitable[Worker]],
    create_ingester: Callable[[Ingester], Awaitable[Ingester]],
    stream_eiger: Callable[[zmq.Context[Any], int, int], Coroutine[Any, Any, None]],
    stream_orca: Callable[[zmq.Context[Any], int, int], Coroutine[Any, Any, None]],
    stream_small: Callable[[zmq.Context[Any], int, int], Coroutine[Any, Any, None]],
    tmp_path: Any,
) -> None:
    p_eiger, p_prefix, uuid = await dump_data(
        reducer,
        create_worker,
        create_ingester,
        stream_eiger,
        stream_orca,
        stream_small,
        tmp_path,
    )

    cbor_file = tmp_path / "binparams.cbor"

    with open(cbor_file, mode="wb") as fb:
        arr = np.ones((10, 10))
        cbor2.dump(
            [
                {"name": "roi1", "data": "[10,20]"},
                {"name": "file_parameter_file", "data": pickle.dumps(arr)},
            ],
            fb,
        )

    stop_event = threading.Event()
    done_event = threading.Event()

    thread = threading.Thread(
        target=replay,
        args=(
            "examples.dummy.worker:FluorescenceWorker",
            "examples.dummy.reducer:FluorescenceReducer",
            [p_eiger, f"{p_prefix}orca-ingester-{uuid}.cbors"],
            None,
            cbor_file,
        ),
        kwargs={"port": 5010, "stop_event": stop_event, "done_event": done_event},
    )
    thread.start()

    logging.info("keep webserver alive")
    done_event.wait()
    logging.info("replay done")

    def work_second() -> None:
        f = h5pyd.File("http://localhost:5010/", "r")
        logging.info("file %s", list(f.keys()))
        logging.info("map %s", list(f["map"].keys()))
        assert list(f.keys()) == ["map"]

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, work_second)

    logging.info("shut down server")
    stop_event.set()

    thread.join()
