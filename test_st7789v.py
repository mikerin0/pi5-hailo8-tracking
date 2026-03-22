from luma.core.interface.serial import spi
from luma.lcd.device import st7789
from PIL import Image, ImageDraw
import time

serial = spi(port=0, device=0, gpio_DC=25, gpio_RST=27, gpio_LIGHT=18)
device = st7789(serial, width=320, height=240, rotate=0)

image = Image.new("RGB", (device.width, device.height), (0, 0, 0))
draw = ImageDraw.Draw(image)

# Draw orientation text in each corner and center
draw.text((5, 5), "TOP LEFT", fill="red")
draw.text((device.width - 80, 5), "TOP RIGHT", fill="green")
draw.text((5, device.height - 20), "BOTTOM LEFT", fill="blue")
draw.text((device.width - 110, device.height - 20), "BOTTOM RIGHT", fill="yellow")
draw.text((device.width // 2 - 40, device.height // 2 - 10), "CENTER", fill="white")

device.display(image)
time.sleep(10)