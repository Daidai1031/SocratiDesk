"""
SocratiDesk — QR Code Upload Flow (Raspberry Pi + MiniPiTFT 1.14")

Shows QR code on Adafruit MiniPiTFT 240x135 ST7789 display.
User scans with phone -> uploads PDF -> Pi gets notified.

Hardware: Adafruit Mini PiTFT 1.14" 240x135 (ST7789)

Usage:
    python qr_upload.py [--device-id socratiDesk-001]

Requires:
    pip install qrcode Pillow websockets aiohttp python-dotenv
    pip install adafruit-circuitpython-st7789 adafruit-circuitpython-rgb-display
"""

import argparse
import asyncio
import json
import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

HTTP_URL = os.getenv("SOCRATIDESK_HTTP", "https://live-server-3234073392.us-central1.run.app")
DEVICE_ID = os.getenv("DEVICE_ID", "socratiDesk-001")


def generate_upload_url(device_id):
    return f"{HTTP_URL}/upload?session={device_id}"


def show_qr_on_tft(url):
    """Display QR code on ST7789 TFT using the same pin config that already works."""
    spi = None
    cs_pin = None
    dc_pin = None
    reset_pin = None
    backlight = None

    try:
        print("  [TFT] importing display libs...")
        import board
        import digitalio
        from PIL import Image, ImageDraw, ImageFont
        from adafruit_rgb_display import st7789
        import qrcode
        print("  [TFT] imports OK")

        print("  [TFT] creating pins...")
        cs_pin = digitalio.DigitalInOut(board.D5)
        print("  [TFT] cs_pin OK")

        dc_pin = digitalio.DigitalInOut(board.D25)
        print("  [TFT] dc_pin OK")

        backlight = digitalio.DigitalInOut(board.D22)
        backlight.switch_to_output()
        backlight.value = True
        print("  [TFT] backlight ON")

        reset_pin = None
        BAUDRATE = 24000000

        print("  [TFT] creating SPI...")
        spi = board.SPI()
        print("  [TFT] SPI OK")

        print("  [TFT] creating ST7789 display...")
        display = st7789.ST7789(
            spi,
            cs=cs_pin,
            dc=dc_pin,
            rst=reset_pin,
            baudrate=BAUDRATE,
            width=135,
            height=240,
            x_offset=53,
            y_offset=40,
        )
        print("  [TFT] display init OK")

        print("  [TFT] generating QR...")
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=3,
            border=2,
        )
        qr.add_data(url)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

        print("  [TFT] building canvas...")
        # 注意：这里的画布尺寸沿用你当前已经能 push 成功的版本
        img = Image.new("RGB", (240, 135), (0, 0, 0))
        draw = ImageDraw.Draw(img)

        qr_size = 131
        qr_img = qr_img.resize((qr_size, qr_size), Image.NEAREST)
        img.paste(qr_img, (2, 2))

        try:
            font_big = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 13
            )
            font_sm = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 10
            )
        except Exception:
            font_big = ImageFont.load_default()
            font_sm = font_big

        text_x = qr_size + 8
        draw.text((text_x, 10), "SocratiDesk", font=font_big, fill=(100, 220, 160))
        draw.text((text_x, 30), "Scan to", font=font_sm, fill=(255, 255, 255))
        draw.text((text_x, 44), "upload", font=font_sm, fill=(255, 255, 255))
        draw.text((text_x, 58), "textbook", font=font_sm, fill=(255, 255, 255))
        draw.text((text_x, 80), "Waiting...", font=font_sm, fill=(180, 180, 100))

        print("  [TFT] pushing image to display...")
        display.image(img, 90)
        print("  [TFT] QR code displayed on screen")

        return display, img, draw, font_sm, text_x, spi, cs_pin, dc_pin, reset_pin, backlight

    except ImportError as e:
        print(f"  [TFT] Import error: {e}")
        return None, None, None, None, None, None, None, None, None, None

    except Exception as e:
        print(f"  [TFT] Display error: {e}")

        for obj_name, obj in [
            ("cs_pin", cs_pin),
            ("dc_pin", dc_pin),
            ("reset_pin", reset_pin),
            ("backlight", backlight),
        ]:
            try:
                if obj is not None:
                    obj.deinit()
                    print(f"  [TFT] cleaned {obj_name}")
            except Exception:
                pass

        try:
            if spi is not None:
                spi.deinit()
                print("  [TFT] cleaned spi")
        except Exception:
            pass

        return None, None, None, None, None, None, None, None, None, None


def update_tft_status(display, img, draw, font, text_x, status, color=(100, 220, 160)):
    """Update the status text on the TFT display."""
    if display is None:
        return
    try:
        draw.rectangle([text_x, 75, 239, 134], fill=(0, 0, 0))
        draw.text((text_x, 80), status, font=font, fill=color)
        display.image(img, 90)
    except Exception:
        pass


def cleanup_tft(display, spi, cs_pin, dc_pin, reset_pin, backlight):
    """Blank the display and release GPIO/SPI resources."""
    try:
        if display is not None:
            from PIL import Image
            blank = Image.new("RGB", (240, 135), (0, 0, 0))
            display.image(blank, 90)
    except Exception:
        pass

    try:
        if backlight is not None:
            backlight.value = False
    except Exception:
        pass

    for obj in [cs_pin, dc_pin, reset_pin, backlight]:
        try:
            if obj is not None:
                obj.deinit()
        except Exception:
            pass

    try:
        if spi is not None:
            spi.deinit()
    except Exception:
        pass


def print_qr_terminal(url):
    """Print QR code to terminal as fallback."""
    try:
        import qrcode
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=1,
            border=2,
        )
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except ImportError:
        print(f"\n  Scan this URL:\n  {url}\n")
    except Exception:
        print(f"\n  Scan this URL:\n  {url}\n")


async def listen_for_upload_ws(device_id):
    import websockets
    ws_proto = "wss" if HTTP_URL.startswith("https") else "ws"
    ws_url = f"{ws_proto}://{HTTP_URL.split('://')[1]}/upload-notify?session={device_id}"
    print(f"  [WS] Connecting to {ws_url}")
    try:
        async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20) as ws:
            print("  [WS] Listening for upload...")
            while True:
                raw = await ws.recv()
                msg = json.loads(raw)
                if msg.get("type") == "textbook_uploaded":
                    return msg
    except Exception as e:
        print(f"  [WS] Error: {e}")
        return {}


async def listen_for_upload_poll(device_id):
    import aiohttp
    url = f"{HTTP_URL}/wait-for-upload?device_id={device_id}"
    print(f"  [POLL] Long-polling {url}")
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("type") == "textbook_uploaded":
                            return data
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                print(f"  [POLL] Error: {e}")
                await asyncio.sleep(3)


async def wait_for_upload(device_id):
    try:
        import websockets
        return await listen_for_upload_ws(device_id)
    except ImportError:
        return await listen_for_upload_poll(device_id)
    except Exception:
        print("  [INFO] WebSocket failed, using long-poll")
        return await listen_for_upload_poll(device_id)


async def main(device_id):
    upload_url = generate_upload_url(device_id)

    print()
    print("  SocratiDesk - Textbook Upload")
    print("  " + "=" * 40)
    print(f"  URL: {upload_url}")
    print()

    display = img = draw = font = text_x = None
    spi = cs_pin = dc_pin = reset_pin = backlight = None

    try:
        # Show QR on TFT display
        display, img, draw, font, text_x, spi, cs_pin, dc_pin, reset_pin, backlight = show_qr_on_tft(upload_url)

        # Also print to terminal
        print_qr_terminal(upload_url)

        print("  Waiting for textbook upload...")
        print("  (Scan QR code with your phone)")
        print()

        # Wait for upload notification
        result = await wait_for_upload(device_id)

        if result:
            name = result.get("name", "unknown")
            pages = result.get("pages", "?")
            chunks = result.get("chunks", "?")
            book_id = result.get("book_id", "")

            print()
            print("  " + "=" * 40)
            print("  Textbook received!")
            print(f"  Name:   {name}")
            print(f"  Pages:  {pages}")
            print(f"  Chunks: {chunks}")
            print("  " + "=" * 40)
            print()
            print("  Say 'textbook mode' to start studying!")
            print()

            update_tft_status(display, img, draw, font, text_x, "Uploaded!", (100, 220, 160))
            return result

        print("  No upload received.")
        return {}

    finally:
        cleanup_tft(display, spi, cs_pin, dc_pin, reset_pin, backlight)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SocratiDesk QR Upload")
    parser.add_argument("--device-id", default=DEVICE_ID)
    args = parser.parse_args()
    asyncio.run(main(args.device_id))