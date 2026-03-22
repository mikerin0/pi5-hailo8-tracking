from luma.core.interface.serial import spi
from luma.lcd.device import st7789
from PIL import Image, ImageDraw
import time

serial = spi(port=0, device=0, gpio_DC=25, gpio_RST=27, gpio_LIGHT=18)
device = st7789(serial, width=240, height=320, rotate=0)

image = Image.new("RGB", (device.width, device.height), (0, 0, 0))
draw = ImageDraw.Draw(image)

box_width = 160
box_height = 120
x0 = (device.width - box_width) // 2
y0 = (device.height - box_height) // 2
x1 = x0 + box_width
y1 = y0 + box_height

draw.rectangle([x0, y0, x1, y1], outline="yellow", width=5)
draw.text((x0 + 20, y0 + 50), "ST7789V OK!", fill="white")

device.display(image)
time.sleep(10)