from time import time
from threading import Thread, Event

import psutil
from pathlib2 import Path
from typing import Text

try:
    import gpustat
except ImportError:
    gpustat = None


class ResourceMonitor(object):
    _title_machine = ':monitor:machine'
    _title_gpu = ':monitor:gpu'

    def __init__(self, task, measure_frequency_times_per_sec=2., report_frequency_sec=30.):
        self._task = task
        self._measure_frequency = measure_frequency_times_per_sec
        self._report_frequency = report_frequency_sec
        self._num_readouts = 0
        self._readouts = {}
        self._previous_readouts = {}
        self._previous_readouts_ts = time()
        self._thread = None
        self._exit_event = Event()
        if not gpustat:
            self._task.get_logger().console('TRAINS Monitor: GPU monitoring is not available, '
                                            'run \"pip install gpustat\"')

    def start(self):
        self._exit_event.clear()
        self._thread = Thread(target=self._daemon, daemon=True)
        self._thread.start()

    def stop(self):
        self._exit_event.set()
        # self._thread.join()

    def _daemon(self):
        logger = self._task.get_logger()
        seconds_since_started = 0
        while True:
            last_report = time()
            while (time() - last_report) < self._report_frequency:
                # wait for self._measure_frequency seconds, if event set quit
                if self._exit_event.wait(1.0 / self._measure_frequency):
                    return
                # noinspection PyBroadException
                try:
                    self._update_readouts()
                except Exception:
                    pass

            average_readouts = self._get_average_readouts()
            seconds_since_started += int(round(time() - last_report))
            for k, v in average_readouts.items():
                # noinspection PyBroadException
                try:
                    title = self._title_gpu if k.startswith('gpu_') else self._title_machine
                    # 3 points after the dot
                    value = round(v*1000) / 1000.
                    logger.report_scalar(title=title, series=k, iteration=seconds_since_started, value=value)
                except Exception:
                    pass
            self._clear_readouts()

    def _update_readouts(self):
        readouts = self._machine_stats()
        elapsed = time() - self._previous_readouts_ts
        self._previous_readouts_ts = time()
        for k, v in readouts.items():
            # cumulative measurements
            if k.endswith('_mbs'):
                v = (v - self._previous_readouts.get(k, v)) / elapsed

            self._readouts[k] = self._readouts.get(k, 0.0) + v
        self._num_readouts += 1
        self._previous_readouts = readouts

    def _get_num_readouts(self):
        return self._num_readouts

    def _get_average_readouts(self):
        average_readouts = dict((k, v/float(self._num_readouts)) for k, v in self._readouts.items())
        return average_readouts

    def _clear_readouts(self):
        self._readouts = {}
        self._num_readouts = 0

    @staticmethod
    def _machine_stats():
        """
        :return: machine stats dictionary, all values expressed in megabytes
        """
        cpu_usage = [float(v) for v in psutil.cpu_percent(percpu=True)]
        stats = {
            "cpu_usage": sum(cpu_usage) / float(len(cpu_usage)),
        }

        bytes_per_megabyte = 1024 ** 2

        def bytes_to_megabytes(x):
            return x / bytes_per_megabyte

        virtual_memory = psutil.virtual_memory()
        stats["memory_used_gb"] = bytes_to_megabytes(virtual_memory.used) / 1024
        stats["memory_free_gb"] = bytes_to_megabytes(virtual_memory.available) / 1024
        disk_use_percentage = psutil.disk_usage(Text(Path.home())).percent
        stats["disk_free_percent"] = 100.0-disk_use_percentage
        sensor_stat = (
            psutil.sensors_temperatures() if hasattr(psutil, "sensors_temperatures") else {}
        )
        if "coretemp" in sensor_stat and len(sensor_stat["coretemp"]):
            stats["cpu_temperature"] = max([float(t.current) for t in sensor_stat["coretemp"]])

        # update cached measurements
        net_stats = psutil.net_io_counters()
        stats["network_tx_mbs"] = bytes_to_megabytes(net_stats.bytes_sent)
        stats["network_rx_mbs"] = bytes_to_megabytes(net_stats.bytes_recv)
        io_stats = psutil.disk_io_counters()
        stats["io_read_mbs"] = bytes_to_megabytes(io_stats.read_bytes)
        stats["io_write_mbs"] = bytes_to_megabytes(io_stats.write_bytes)

        # check if we can access the gpu statistics
        if gpustat:
            gpu_stat = gpustat.new_query()
            for i, g in enumerate(gpu_stat.gpus):
                stats["gpu_%d_temperature" % i] = float(g["temperature.gpu"])
                stats["gpu_%d_utilization" % i] = float(g["utilization.gpu"])
                stats["gpu_%d_mem_usage" % i] = 100. * float(g["memory.used"]) / float(g["memory.total"])
                # already in MBs
                stats["gpu_%d_mem_free_gb" % i] = float(g["memory.total"] - g["memory.used"]) / 1024
                stats["gpu_%d_mem_used_gb" % i] = float(g["memory.used"]) / 1024

        return stats
