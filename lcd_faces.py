from luma.core.interface.serial import spi
from luma.lcd.device import st7789
from PIL import Image, ImageDraw

# Initialize LCD only once
_lcd_serial = None
_lcd_device = None

def get_lcd_device():
    global _lcd_serial, _lcd_device
    if _lcd_device is not None:
        return _lcd_device
    _lcd_serial = spi(port=0, device=0, gpio_DC=25, gpio_RST=27, gpio_LIGHT=18)
    _lcd_device = st7789(_lcd_serial, width=320, height=240, rotate=0)
    return _lcd_device

def draw_happy_face():
    lcd = get_lcd_device()
    from PIL import Image
    try:
        image = Image.open("/home/arm/faces/happy-face.png").convert("RGB")
        # Resize and center if needed
        image = image.resize((lcd.width, lcd.height), Image.LANCZOS)
        lcd.display(image)
    except Exception as e:
        print(f"[LCD] Could not display happy-face.png: {e}")

def draw_sad_face():
    lcd = get_lcd_device()
    from PIL import Image
    try:
        image = Image.open("/home/arm/faces/sad-face.png").convert("RGB")
        # Resize and center if needed
        image = image.resize((lcd.width, lcd.height), Image.LANCZOS)
        lcd.display(image)
    except Exception as e:
        print(f"[LCD] Could not display happy-face.png: {e}")

def draw_thinking_face():
    lcd = get_lcd_device()
    from PIL import Image
    try:
        image = Image.open("/home/arm/faces/thinking-face.png").convert("RGB")
        # Resize and center if needed
        image = image.resize((lcd.width, lcd.height), Image.LANCZOS)
        lcd.display(image)
    except Exception as e:
        print(f"[LCD] Could not display happy-face.png: {e}")

def draw_sleeping_face():
    lcd = get_lcd_device()
    from PIL import Image
    try:
        image = Image.open("/home/arm/faces/sleeping-face.png").convert("RGB")
        # Resize and center if needed
        image = image.resize((lcd.width, lcd.height), Image.LANCZOS)
        lcd.display(image)
    except Exception as e:
        print(f"[LCD] Could not display happy-face.png: {e}")

def draw_mad_face():
    lcd = get_lcd_device()
    from PIL import Image
    try:
        image = Image.open("/home/arm/faces/mad-face.png").convert("RGB")
        # Resize and center if needed
        image = image.resize((lcd.width, lcd.height), Image.LANCZOS)
        lcd.display(image)
    except Exception as e:
        print(f"[LCD] Could not display happy-face.png: {e}")
