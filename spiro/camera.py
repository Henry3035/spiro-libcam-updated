import time
import io
import threading
import numpy as np
from PIL import Image
# optional, faster encoder
try:
    import cv2
    _have_cv2 = True
except Exception:
    _have_cv2 = False
from spiro.logger import log, debug

class NewCamera:
    def __init__(self):
        debug('Libcamera detected.')
        self.camera = Picamera2()
        self.type = 'libcamera'
        self.streaming = False
        self.stream_output = None
        self._rotation = 0  # rotation in degrees, clockwise
        self._stream_thread = None
        self._stream_thread_stop = threading.Event()

#	self.preview_mode = None

        self.still_config = self.camera.create_still_configuration(main={"size": (4656, 3496)}, lores={"size": (320, 240)}, raw = None)
        self.video_config = self.camera.create_video_configuration(main={"size": (1640,1232)}) # original 1024,768

        try:
            self.camera.configure(self.video_config)
        except Exception:
            pass

        # camera_controls may vary; attempt to get lens limits if present
        try:
            controls_map = getattr(self.camera, 'camera_controls', {})
            self.lens_limits = controls_map.get('LensPosition', None)
        except Exception:
            self.lens_limits = None

        try:
            self.camera.set_controls({
                'NoiseReductionMode': controls.draft.NoiseReductionModeEnum.Off,
                'AeMeteringMode': controls.AeMeteringModeEnum.Spot,
                "AfMode": controls.AfModeEnum.Manual,
                "LensPosition": (self.lens_limits[2] if self.lens_limits and len(self.lens_limits) > 2 else 0)
            })
        except Exception:
            # be lenient if controls or enums are not available
            pass

        try:
            self.camera.start()
        except Exception:
            pass

    def start_stream(self, output):
        log('Starting stream.')
        try:
            self.stream_output = output
            self._stream_thread_stop.clear()
            self.streaming = True
            self.camera.switch_mode(self.video_config)
            # if no rotation requested, use efficient MJPEGEncoder path
            if self._rotation % 360 == 0:
                try:
                    self.camera.start_recording(MJPEGEncoder(), FileOutput(output))
                except Exception:
                    debug('Failed to start MJPEG stream, falling back to frame loop', exc_info=True)
                    self._start_frame_stream(output)
            else:
                # rotation requested, use frame loop stream that rotates frames
                self._start_frame_stream(output)
        except Exception:
            debug('Failed to start stream', exc_info=True)

    def _start_frame_stream(self, output):
        # run a thread capturing frames, rotating if necessary, and writing MJPEG frames into output
        def stream_loop():
            while not self._stream_thread_stop.is_set():
                try:
                    frame = self.camera.capture_array()

                    # rotate frame clockwise by _rotation degrees (only multiples of 90 supported)
                    if self._rotation % 360 == 90:
                        frame = np.rot90(frame, k=-1)
                    elif self._rotation % 360 == 180:
                        frame = np.rot90(frame, k=2)
                    elif self._rotation % 360 == 270:
                        frame = np.rot90(frame, k=1)

                    # convert to JPEG (use OpenCV encoder if available for speed)
                    if _have_cv2:
                        try:
                            # OpenCV expects BGR
                            frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                            ret, buf = cv2.imencode('.jpg', frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
                            if ret:
                                jpg = buf.tobytes()
                                with output.condition:
                                    output.frame = jpg
                                    output.condition.notify_all()
                                continue
                        except Exception:
                            # fall back to PIL if cv2 fails
                            debug('cv2 encoding failed, falling back to PIL', exc_info=True)

                    # PIL fallback
                    try:
                        im = Image.fromarray(frame)
                        if im.mode != 'RGB':
                            im = im.convert('RGB')
                        buf = io.BytesIO()
                        im.save(buf, format='JPEG', quality=85)
                        jpg = buf.getvalue()
                        with output.condition:
                            output.frame = jpg
                            output.condition.notify_all()
                    except Exception:
                        import traceback
                        debug('Frame stream loop error')
                        debug(traceback.format_exc())
                except Exception:
                    import traceback
                    debug('Outer frame stream loop error')
                    debug(traceback.format_exc())

                time.sleep(0.03)

        self._stream_thread = threading.Thread(target=stream_loop, daemon=True)
        self._stream_thread.start()


#    def start_stream(self, output):
#        log('Starting stream.')
#        self.streaming = True
#        self.stream_output = output
#        self.camera.switch_mode(self.video_config)

#        def stream_loop():
#            while self.streaming:
#                frame = self.camera.capture_array()
#                frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
#                ret, buffer = cv2.imencode('.jpg', frame)
#                if ret:
#                    with output.condition:
#                        output.frame = buffer.tobytes()
#                        output.condition.notify_all()
#                time.sleep(0.03)  # ~30 FPS

#        t = Thread(target=stream_loop, daemon=True)
#        t.start()




    def stop_stream(self):
        # stop either recording or frame-loop thread
        try:
            # stop frame-loop thread if running
            if self._stream_thread and self._stream_thread.is_alive():
                self._stream_thread_stop.set()
                self._stream_thread.join(timeout=2)
                self._stream_thread = None
            # try to stop recording if picamera2 is using start_recording
            try:
                self.camera.stop_recording()
            except Exception:
                pass
        except Exception:
            debug('Error stopping stream', exc_info=True)


    @property
    def zoom(self):
        # not implemented for libcamera wrapper
        return None

    @zoom.setter
    def zoom(self, value):
        """Accepts a tuple (x, y, w, h) where values are fractions of the full sensor size."""
        try:
            x, y, w, h = value
        except Exception:
            raise ValueError('zoom setter expects a tuple (x, y, w, h)')

        try:
            (resx, resy) = self.camera.camera_properties.get('PixelArraySize', (0, 0))
            self.camera.set_controls({"ScalerCrop": (int(x * resx), int(y * resy), int(w * resx), int(h * resy))})
        except Exception:
            debug('Failed to set zoom', exc_info=True)

    def auto_exposure(self, value):
        try:
            self.camera.set_controls({'AeEnable': value})
        except Exception:
            debug('Failed to set auto_exposure', exc_info=True)

    def capture(self, obj, format='png'):
        stream = self.streaming

        log('Capturing image.')
        try:
            self.camera.switch_mode(self.still_config)
            self.camera.capture_file(obj, format=format)
            # if rotation requested, rotate the saved image (clockwise)
            if self._rotation % 360 != 0:
                try:
                    obj.seek(0)
                    im = Image.open(obj)
                    if self._rotation % 360 == 90:
                        im = im.transpose(Image.ROTATE_270)
                    elif self._rotation % 360 == 180:
                        im = im.transpose(Image.ROTATE_180)
                    elif self._rotation % 360 == 270:
                        im = im.transpose(Image.ROTATE_90)
                    obj.truncate(0); obj.seek(0)
                    im.save(obj, format=format.upper())
                    obj.seek(0)
                except Exception:
                    debug('Failed to rotate still image', exc_info=True)
            log('Ok.')
        except Exception:
            debug('Capture failed', exc_info=True)

        if stream:
            self.start_stream(self.stream_output)

    @property
    def shutter_speed(self):
        try:
            return self.camera.capture_metadata().get('ExposureTime')
        except Exception:
            return None

    @shutter_speed.setter
    def shutter_speed(self, value):
        try:
            self.camera.set_controls({"ExposureTime": value})
        except Exception:
            debug('Failed to set shutter_speed', exc_info=True)

    @property
    def iso(self):
        try:
            return int(self.camera.capture_metadata().get('AnalogueGain', 1) * 100)
        except Exception:
            return None

    @iso.setter
    def iso(self, value):
        try:
            self.camera.set_controls({"AnalogueGain": value / 100})
        except Exception:
            debug('Failed to set iso', exc_info=True)

    def close(self):
        try:
            self.camera.close()
        except Exception:
            pass

    def still_mode(self):
        try:
            self.camera.switch_mode(self.still_config)
        except Exception:
            pass

    def video_mode(self):
        try:
            self.camera.switch_mode(self.video_config)
        except Exception:
            pass

    @property
    def resolution(self):
        # best-effort: return still resolution if available
        try: 
            return self.camera.sensor_resolution
        except:
            return (4608, 3456)

    @resolution.setter
    def resolution(self, res):
        # not implemented for libcamera wrapper here
        pass

    @property
    def awb_mode(self):
        return None

    @awb_mode.setter
    def awb_mode(self, mode):
        pass

    @property
    def awb_gains(self):
        return None

    @awb_gains.setter
    def awb_gains(self, gains):
        pass

    def focus(self, val):
        try:
            self.camera.set_controls({'LensPosition': val})
        except Exception:
            debug('Failed to set focus', exc_info=True)

    @property
    def rotation(self):
        return self._rotation

    @rotation.setter
    def rotation(self, degrees):
        try:
            self._rotation = int(degrees) % 360
        except Exception:
            debug('Invalid rotation value', exc_info=True)

from picamera2 import Picamera2
from picamera2.outputs import FileOutput
from picamera2.encoders import MJPEGEncoder
from libcamera import controls

cam = NewCamera()
