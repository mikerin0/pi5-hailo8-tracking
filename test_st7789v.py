from luma.core.interface.serial import spi
from luma.lcd.device import st7789
from PIL import Image, ImageDraw
import time

serial = spi(port=0, device=0, gpio_DC=25, gpio_RST=27, gpio_LIGHT=18)
device = st7789(serial, width=320, height=240, rotate=0)

# Create a blank image
image = Image.new("RGB", (device.width, device.height), (0, 0, 0))
draw = ImageDraw.Draw(image)

# Center coordinates
cx, cy = device.width // 2, device.height // 2
face_radius = 80
eye_radius = 10
eye_offset_x = 30
eye_offset_y = 25
smile_radius = 40

# Draw face (yellow circle)
draw.ellipse([
	(cx - face_radius, cy - face_radius),
	(cx + face_radius, cy + face_radius)
], fill="yellow", outline="orange", width=4)

# Draw eyes (black circles)
draw.ellipse([
	(cx - eye_offset_x - eye_radius, cy - eye_offset_y - eye_radius),
	(cx - eye_offset_x + eye_radius, cy - eye_offset_y + eye_radius)
], fill="black")
draw.ellipse([
	(cx + eye_offset_x - eye_radius, cy - eye_offset_y - eye_radius),
	(cx + eye_offset_x + eye_radius, cy - eye_offset_y + eye_radius)
], fill="black")

# Draw smile (arc)
smile_box = [
	(cx - smile_radius, cy - 10),
	(cx + smile_radius, cy + 50)
]
draw.arc(smile_box, start=20, end=160, fill="black", width=5)

device.display(image)
time.sleep(10)