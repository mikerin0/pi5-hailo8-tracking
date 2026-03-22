from luma.core.interface.serial import spi
from luma.lcd.device import st7789
from PIL import Image, ImageDraw
import time

serial = spi(port=0, device=0, gpio_DC=25, gpio_RST=27, gpio_LIGHT=18)
device = st7789(serial, width=240, height=320, rotate=0)

bar_width = 20
steps = device.width - bar_width
for x in range(0, steps, 5):
	image = Image.new("RGB", (device.width, device.height), (0, 0, 0))
	draw = ImageDraw.Draw(image)
	draw.rectangle([x, 0, x + bar_width, device.height], fill=(0, 255, 0))
	device.display(image)
	time.sleep(0.03)

time.sleep(1)