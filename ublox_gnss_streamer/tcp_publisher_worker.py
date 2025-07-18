import threading
from threading import Event
import socket
from datetime import datetime, timezone, timedelta

from .tcp_publisher import TcpPublisher
from ublox_gnss_streamer.utils.logger import logger
from ublox_gnss_streamer.utils.schemas import GnssDataSchema
from ublox_gnss_streamer.utils.threadsafe_deque import ThreadSafeDeque

class TcpPublisherWorker:
    def __init__(
        self, 
        publisher: TcpPublisher, 
        stop_event: Event,
        gnss_queue: ThreadSafeDeque = None,
        broadcast_interval: float = 0.01,  # Default broadcast interval
    ):
        self.publisher = publisher
        self.gnss_queue = gnss_queue
        self.stop_event = stop_event
        self.publisher_lock = threading.Lock()
        self.accept_thread = None
        self.broadcast_thread = None
        self.broadcast_interval = broadcast_interval  # Interval for broadcasting data
    
    def run(self):
        self.publisher.start_server()
        self.accept_thread = threading.Thread(target=self._accept_clients_loop, daemon=True)
        self.accept_thread.start()
        self.broadcast_thread = threading.Thread(target=self._broadcast_data_loop, daemon=True)
        self.broadcast_thread.start()
        logger.info("TCP Publisher worker started.")
        return True
    
    def _accept_clients_loop(self):
        while not self.stop_event.is_set():
            try:
                self.publisher.server_socket.settimeout(1.0)
                try:
                    self.publisher.accept_client()
                except socket.timeout:
                    continue
                with self.publisher_lock:
                    self.publisher.refresh_clients()
            except Exception as e:
                logger.error(f"Error accepting client: {e}", exc_info=True)
    
    def _broadcast_data_loop(self):
        
        KST = timezone(timedelta(hours=9))

        while not self.stop_event.is_set():
            if self.stop_event.wait(self.broadcast_interval):
                break
            
            if self.gnss_queue is not None and len(self.gnss_queue) > 0:
                raw = self.gnss_queue.popleft()

                # Determine 'type' field
                if raw.get("extrapolated"):
                    type_str = "extrapolated"
                elif not raw.get("gnssFixOk"):
                    type_str = "no-fix"
                elif raw.get("fixType") == 3 and raw.get("carrSoln") == 2:
                    type_str = "fixed-rtk"
                elif raw.get("fixType") == 3 and raw.get("carrSoln") == 1:
                    type_str = "float-rtk"
                elif raw.get("fixType") == 3 and raw.get("carrSoln") == 0:
                    type_str = "no-rtk"
                elif raw.get("fixType") == 4:
                    type_str = "dead-reckoning"
                else:
                    type_str = "no-rtk"

                # Convert timestamp to KST
                ts = raw["timestamp"]
                if isinstance(ts, datetime):
                    ts = ts.astimezone(KST)
                else:
                    ts = datetime.fromtimestamp(ts, tz=KST)
                    
                # Build the schema
                gnss_data = GnssDataSchema(
                    timestamp=ts,
                    gnss_time=str(raw["gnss_time"]),
                    lat=raw["lat"],
                    lon=raw["lon"],
                    alt=raw["height"],
                    type=type_str,
                    # fixType=raw.get("fixType"),
                    # carrSoln=raw.get("carrSoln"),
                    # gnssFixOk=raw.get("gnssFixOk"),
                    # extrapolationError=raw.get("extrapolationError")
                )

                with self.publisher_lock:
                    self.publisher.send_to_all(
                        data=gnss_data.json().encode('utf-8') + b'\n'
                    )
                    
    
    def stop(self):
        self.stop_event.set()
        if self.accept_thread:
            self.accept_thread.join()
        if self.broadcast_thread:
            self.broadcast_thread.join()
        with self.publisher_lock:
            self.publisher.stop_server()
        logger.info("TCP Publisher worker stopped.")
        
