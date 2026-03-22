from luma.core.interface.serial import spi
from luma.lcd.device import st7789
from PIL import Image
import time

serial = spi(port=0, device=0, gpio_DC=25, gpio_RST=27, gpio_LIGHT=18)
device = st7789(serial, width=240, height=320, rotate=0)

colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255), (0, 0, 0)]  # Red, Green, Blue, White, Black
color_names = ["Red", "Green", "Blue", "White", "Black"]

for i, color in enumerate(colors):
	image = Image.new("RGB", (device.width, device.height), color)
	device.display(image)
	print(f"Displaying {color_names[i]}")
	time.sleep(2)