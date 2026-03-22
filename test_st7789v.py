from luma.core.interface.serial import spi
from luma.lcd.device import st7789
from PIL import Image, ImageDraw
import time

serial = spi(port=0, device=0, gpio_DC=25, gpio_RST=27, gpio_LIGHT=18)
device = st7789(serial, width=240, height=135, rotate=0)

image = Image.new("RGB", (device.width, device.height), (0, 0, 0))
draw = ImageDraw.Draw(image)
draw.rectangle([20, 20, device.width-20, device.height-20], outline="yellow", width=5)
draw.text((60, 60), "ST7789V OK!", fill="white")

device.display(image)
time.sleep(10)  # Image remains visible for 10 seconds
