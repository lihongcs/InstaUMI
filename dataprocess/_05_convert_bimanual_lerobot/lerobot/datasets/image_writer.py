#!/usr/bin/env python

# Copyright 2024 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import multiprocessing
import os
import queue
import threading
import traceback
from concurrent.futures import Future, ThreadPoolExecutor, wait
from io import BytesIO
from pathlib import Path

import numpy as np
import PIL.Image
import torch


def png_compress_level() -> int:
    """Return the lossless PNG compression level used by offline conversion.

    PNG compression changes only storage size and CPU time; decoded uint8 pixels
    are identical for every valid level.  Pillow's default level 6 is tuned for
    size, not throughput.  Photographic 224x224 frames in this pipeline encode
    several times faster at level 1 with comparable size, so use that as the
    production default while retaining an environment override for audits.
    """

    raw = os.environ.get("INSTAUMI_PNG_COMPRESS_LEVEL", "1")
    try:
        level = int(raw)
    except ValueError as exc:
        raise ValueError(f"INSTAUMI_PNG_COMPRESS_LEVEL must be an integer, got {raw!r}") from exc
    if not 0 <= level <= 9:
        raise ValueError(f"INSTAUMI_PNG_COMPRESS_LEVEL must be in [0, 9], got {level}")
    return level


def safe_stop_image_writer(func):
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            dataset = kwargs.get("dataset")
            image_writer = getattr(dataset, "image_writer", None) if dataset else None
            if image_writer is not None:
                print("Waiting for image writer to terminate...")
                image_writer.stop()
            raise e

    return wrapper


def image_array_to_pil_image(image_array: np.ndarray, range_check: bool = True) -> PIL.Image.Image:
    # TODO(aliberts): handle 1 channel and 4 for depth images
    if image_array.ndim != 3:
        raise ValueError(f"The array has {image_array.ndim} dimensions, but 3 is expected for an image.")

    if image_array.shape[0] == 3:
        # Transpose from pytorch convention (C, H, W) to (H, W, C)
        image_array = image_array.transpose(1, 2, 0)

    elif image_array.shape[-1] not in (1, 3):
        raise NotImplementedError(
            f"The image has {image_array.shape[-1]} channels, but 1 or 3 is required for now."
        )

    if image_array.shape[-1] == 1:
        image_array = image_array.squeeze(-1)

    if image_array.dtype != np.uint8:
        if image_array.dtype in (np.float32, np.float64):
            if range_check:
                max_ = image_array.max().item()
                min_ = image_array.min().item()
                if max_ > 1.0 or min_ < 0.0:
                    pass # Bypass for depth maps

            # Since float arrays cause PIL png save to fail, and float depth is commonly
            # represented in meters here (max ~4.0), we scale to millimeters and cast to uint16.
            # uint16 enables mode 'I;16' which can be safely saved as png.
            image_array = (image_array * 1000).astype(np.uint16)
        elif image_array.dtype == np.int32:
            image_array = image_array.astype(np.uint16)



    return PIL.Image.fromarray(image_array)


def write_image(image: np.ndarray | PIL.Image.Image, fpath: Path):
    if isinstance(image, np.ndarray):
        img = image_array_to_pil_image(image)
    elif isinstance(image, PIL.Image.Image):
        img = image
    else:
        raise TypeError(f"Unsupported image type: {type(image)}")
    save_kwargs = {"compress_level": png_compress_level()} if fpath.suffix.lower() == ".png" else {}
    img.save(fpath, **save_kwargs)


def encode_image_png(image: np.ndarray | PIL.Image.Image, virtual_path: Path) -> dict[str, bytes | str]:
    """Encode a lossless PNG in memory using the same Pillow path as ``write_image``."""

    if isinstance(image, np.ndarray):
        img = image_array_to_pil_image(image)
    elif isinstance(image, PIL.Image.Image):
        img = image
    else:
        raise TypeError(f"Unsupported image type: {type(image)}")
    buffer = BytesIO()
    img.save(buffer, format="PNG", compress_level=png_compress_level())
    # datasets.embed_table_storage keeps only the basename after embedding a
    # file-backed image.  Match that representation for schema/reader parity.
    return {"bytes": buffer.getvalue(), "path": virtual_path.name}


def worker_thread_loop(work_queue: queue.Queue, error_queue):
    while True:
        item = work_queue.get()
        if item is None:
            work_queue.task_done()
            break
        image_array, fpath = item
        try:
            write_image(image_array, fpath)
        except Exception:
            error_queue.put(f"{fpath}:\n{traceback.format_exc()}")
        finally:
            work_queue.task_done()


def worker_process(work_queue: queue.Queue, error_queue, num_threads: int):
    threads = []
    for _ in range(num_threads):
        t = threading.Thread(target=worker_thread_loop, args=(work_queue, error_queue))
        t.daemon = True
        t.start()
        threads.append(t)
    for t in threads:
        t.join()


class AsyncImageWriter:
    """
    This class abstract away the initialisation of processes or/and threads to
    save images on disk asynchronously, which is critical to control a robot and record data
    at a high frame rate.

    When `num_processes=0`, it creates a threads pool of size `num_threads`.
    When `num_processes>0`, it creates processes pool of size `num_processes`, where each subprocess starts
    their own threads pool of size `num_threads`.

    The optimal number of processes and threads depends on your computer capabilities.
    We advise to use 4 threads per camera with 0 processes. If the fps is not stable, try to increase or lower
    the number of threads. If it is still not stable, try to use 1 subprocess, or more.
    """

    def __init__(self, num_processes: int = 0, num_threads: int = 1):
        self.num_processes = num_processes
        self.num_threads = num_threads
        self.queue = None
        self.threads = []
        self.processes = []
        self._stopped = False

        if num_threads <= 0 and num_processes <= 0:
            raise ValueError("Number of threads and processes must be greater than zero.")

        default_queue_size = max(64, num_threads * max(1, num_processes) * 16)
        raw_queue_size = os.environ.get("INSTAUMI_IMAGE_QUEUE_SIZE", str(default_queue_size))
        try:
            queue_size = int(raw_queue_size)
        except ValueError as exc:
            raise ValueError(f"INSTAUMI_IMAGE_QUEUE_SIZE must be an integer, got {raw_queue_size!r}") from exc
        if queue_size <= 0:
            raise ValueError(f"INSTAUMI_IMAGE_QUEUE_SIZE must be positive, got {queue_size}")

        if self.num_processes == 0:
            # Use threading
            self.queue = queue.Queue(maxsize=queue_size)
            self.error_queue = queue.Queue()
            for _ in range(self.num_threads):
                t = threading.Thread(target=worker_thread_loop, args=(self.queue, self.error_queue))
                t.daemon = True
                t.start()
                self.threads.append(t)
        else:
            # Use multiprocessing.  Both queues are bounded/observable by the
            # parent so an encoder failure cannot silently produce a dataset
            # with missing images.
            self.queue = multiprocessing.JoinableQueue(maxsize=queue_size)
            self.error_queue = multiprocessing.Queue()
            for _ in range(self.num_processes):
                p = multiprocessing.Process(
                    target=worker_process,
                    args=(self.queue, self.error_queue, self.num_threads),
                )
                p.daemon = True
                p.start()
                self.processes.append(p)

    def save_image(self, image: torch.Tensor | np.ndarray | PIL.Image.Image, fpath: Path):
        if isinstance(image, torch.Tensor):
            # Convert tensor to numpy array to minimize main process time
            image = image.cpu().numpy()
        self.queue.put((image, fpath))

    def wait_until_done(self):
        self.queue.join()
        self._raise_worker_errors()

    def _raise_worker_errors(self):
        errors = []
        while True:
            try:
                errors.append(self.error_queue.get_nowait())
            except queue.Empty:
                break
        if errors:
            detail = "\n".join(errors[:10])
            if len(errors) > 10:
                detail += f"\n... and {len(errors) - 10} more image write failures"
            raise RuntimeError(f"Asynchronous image writer failed:\n{detail}")

    def stop(self):
        if self._stopped:
            return

        if self.num_processes == 0:
            for _ in self.threads:
                self.queue.put(None)
            for t in self.threads:
                t.join()
        else:
            num_nones = self.num_processes * self.num_threads
            for _ in range(num_nones):
                self.queue.put(None)
            for p in self.processes:
                p.join()
                if p.is_alive():
                    p.terminate()
            self.queue.close()
            self.queue.join_thread()

        self._stopped = True
        self._raise_worker_errors()


class AsyncImageEncoder:
    """Bounded thread pool producing embedded PNG byte dictionaries in RAM.

    This removes the write/read/delete cycle for temporary PNG files during
    offline LeRobot conversion.  The encoded bytes and image path basename are
    the same representation stored by Hugging Face ``datasets.Image`` after
    embedding a file-backed PNG.
    """

    def __init__(self, num_threads: int = 1):
        if num_threads <= 0:
            raise ValueError("Number of image encoder threads must be positive.")
        raw_queue_size = os.environ.get("INSTAUMI_IMAGE_QUEUE_SIZE", str(max(64, num_threads * 16)))
        try:
            queue_size = int(raw_queue_size)
        except ValueError as exc:
            raise ValueError(f"INSTAUMI_IMAGE_QUEUE_SIZE must be an integer, got {raw_queue_size!r}") from exc
        if queue_size <= 0:
            raise ValueError(f"INSTAUMI_IMAGE_QUEUE_SIZE must be positive, got {queue_size}")
        self._executor = ThreadPoolExecutor(max_workers=num_threads, thread_name_prefix="png-encoder")
        self._slots = threading.BoundedSemaphore(queue_size)
        self._pending: list[Future] = []
        self._stopped = False

    def save_image(
        self,
        image: torch.Tensor | np.ndarray | PIL.Image.Image,
        virtual_path: Path,
    ) -> Future:
        if self._stopped:
            raise RuntimeError("Image encoder has already stopped")
        if isinstance(image, torch.Tensor):
            image = image.cpu().numpy()
        self._slots.acquire()
        try:
            future = self._executor.submit(encode_image_png, image, virtual_path)
        except Exception:
            self._slots.release()
            raise
        future.add_done_callback(lambda unused_future: self._slots.release())
        self._pending.append(future)
        return future

    def wait_until_done(self) -> None:
        pending, self._pending = self._pending, []
        if not pending:
            return
        wait(pending)
        errors = []
        for future in pending:
            try:
                future.result()
            except Exception:
                errors.append(traceback.format_exc())
        if errors:
            raise RuntimeError("Asynchronous PNG encoding failed:\n" + "\n".join(errors[:10]))

    def stop(self) -> None:
        if self._stopped:
            return
        try:
            self.wait_until_done()
        finally:
            self._executor.shutdown(wait=True, cancel_futures=False)
            self._stopped = True
