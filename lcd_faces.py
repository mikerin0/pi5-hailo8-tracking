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
    image = Image.new("RGB", (lcd.width, lcd.height), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    cx, cy = lcd.width // 2, lcd.height // 2
    face_radius = 80
    eye_radius = 10
    eye_offset_x = 30
    eye_offset_y = 25
    smile_radius = 40
    draw.ellipse([
        (cx - face_radius, cy - face_radius),
        (cx + face_radius, cy + face_radius)
    ], fill="yellow", outline="orange", width=4)
    draw.ellipse([
        (cx - eye_offset_x - eye_radius, cy - eye_offset_y - eye_radius),
        (cx - eye_offset_x + eye_radius, cy - eye_offset_y + eye_radius)
    ], fill="black")
    draw.ellipse([
        (cx + eye_offset_x - eye_radius, cy - eye_offset_y - eye_radius),
        (cx + eye_offset_x + eye_radius, cy - eye_offset_y + eye_radius)
    ], fill="black")
    smile_box = [
        (cx - smile_radius, cy - 10),
        (cx + smile_radius, cy + 50)
    ]
    draw.arc(smile_box, start=20, end=160, fill="black", width=5)
    lcd.display(image)

def draw_sad_face():
    lcd = get_lcd_device()
    image = Image.new("RGB", (lcd.width, lcd.height), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    cx, cy = lcd.width // 2, lcd.height // 2
    face_radius = 80
    eye_radius = 10
    eye_offset_x = 30
    eye_offset_y = 25
    smile_radius = 40
    draw.ellipse([
        (cx - face_radius, cy - face_radius),
        (cx + face_radius, cy + face_radius)
    ], fill="yellow", outline="orange", width=4)
    draw.ellipse([
        (cx - eye_offset_x - eye_radius, cy - eye_offset_y - eye_radius),
        (cx - eye_offset_x + eye_radius, cy - eye_offset_y + eye_radius)
    ], fill="black")
    draw.ellipse([
        (cx + eye_offset_x - eye_radius, cy - eye_offset_y - eye_radius),
        (cx + eye_offset_x + eye_radius, cy - eye_offset_y + eye_radius)
    ], fill="black")
    frown_box = [
        (cx - smile_radius, cy + 30),
        (cx + smile_radius, cy + 90)
    ]
    draw.arc(frown_box, start=200, end=340, fill="black", width=5)
    lcd.display(image)

def draw_thinking_face():
    lcd = get_lcd_device()
    image = Image.new("RGB", (lcd.width, lcd.height), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    cx, cy = lcd.width // 2, lcd.height // 2
    face_radius = 80
    eye_radius = 10
    eye_offset_x = 30
    eye_offset_y = 25
    draw.ellipse([
        (cx - face_radius, cy - face_radius),
        (cx + face_radius, cy + face_radius)
    ], fill="yellow", outline="orange", width=4)
    # Eyes: one open, one half-closed
    draw.ellipse([
        (cx - eye_offset_x - eye_radius, cy - eye_offset_y - eye_radius),
        (cx - eye_offset_x + eye_radius, cy - eye_offset_y + eye_radius)
    ], fill="black")
    draw.line([
        (cx + eye_offset_x - eye_radius, cy - eye_offset_y),
        (cx + eye_offset_x + eye_radius, cy - eye_offset_y)
    ], fill="black", width=4)
    # Mouth: straight line
    draw.line([
        (cx - 25, cy + 50), (cx + 25, cy + 50)
    ], fill="black", width=5)
    # Thought bubble
    draw.ellipse([
        (cx + 60, cy + 80, cx + 80, cy + 100)
    ], outline="gray", width=2)
    draw.ellipse([
        (cx + 80, cy + 100, cx + 110, cy + 120)
    ], outline="gray", width=2)
    lcd.display(image)

def draw_sleeping_face():
    lcd = get_lcd_device()
    image = Image.new("RGB", (lcd.width, lcd.height), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    cx, cy = lcd.width // 2, lcd.height // 2
    face_radius = 80
    eye_radius = 10
    eye_offset_x = 30
    eye_offset_y = 25
    draw.ellipse([
        (cx - face_radius, cy - face_radius),
        (cx + face_radius, cy + face_radius)
    ], fill="yellow", outline="orange", width=4)
    # Eyes: closed (arcs)
    draw.arc([
        (cx - eye_offset_x - eye_radius, cy - eye_offset_y - 2, cx - eye_offset_x + eye_radius, cy - eye_offset_y + 8)
    ], start=0, end=180, fill="black", width=3)
    draw.arc([
        (cx + eye_offset_x - eye_radius, cy - eye_offset_y - 2, cx + eye_offset_x + eye_radius, cy - eye_offset_y + 8)
    ], start=0, end=180, fill="black", width=3)
    # Mouth: small smile
    smile_box = [
        (cx - 20, cy + 40),
        (cx + 20, cy + 60)
    ]
    draw.arc(smile_box, start=20, end=160, fill="black", width=4)
    # Zzz
    draw.text((cx + 60, cy - 60), "Zz", fill="blue")
    lcd.display(image)

def draw_mad_face():
    lcd = get_lcd_device()
    image = Image.new("RGB", (lcd.width, lcd.height), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    cx, cy = lcd.width // 2, lcd.height // 2
    face_radius = 80
    eye_radius = 10
    eye_offset_x = 30
    eye_offset_y = 25
    draw.ellipse([
        (cx - face_radius, cy - face_radius),
        (cx + face_radius, cy + face_radius)
    ], fill="yellow", outline="orange", width=4)
    # Eyes: angry (slanted lines)
    draw.line([
        (cx - eye_offset_x - eye_radius, cy - eye_offset_y - 10),
        (cx - eye_offset_x + eye_radius, cy - eye_offset_y + 10)
    ], fill="black", width=4)
    draw.line([
        (cx + eye_offset_x - eye_radius, cy - eye_offset_y + 10),
        (cx + eye_offset_x + eye_radius, cy - eye_offset_y - 10)
    ], fill="black", width=4)
    # Mouth: frown
    frown_box = [
        (cx - 30, cy + 50),
        (cx + 30, cy + 80)
    ]
    draw.arc(frown_box, start=200, end=340, fill="black", width=5)
    lcd.display(image)
