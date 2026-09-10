import os
import shutil

import subprocess
import json
import time
import tempfile

import sys
import termios
import tty
import fcntl
import board
import busio
from PIL import Image, ImageDraw, ImageFont
import adafruit_ssd1306

import RPi.GPIO as GPIO
import threading



#OLED--------------------------------------------------------------------------------
i2c = busio.I2C(board.SCL, board.SDA)
display = adafruit_ssd1306.SSD1306_I2C(128, 32, i2c)
display_lock = threading.Lock()
scroll_thread = None
scroll_stop_event = threading.Event()
OLED_FRAME_DELAY = 0.15
OLED_PIXEL_STEP = 3
OLED_LOOP_PAUSE = 1.0

# โหลดฟอนต์
font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
font_size = 32  # ปรับขนาดตามที่เหมาะกับจอ
font = ImageFont.truetype(font_path, font_size)

# สร้างพื้นภาพ
def draw_text(text):
    image = Image.new("1", (display.width, display.height))
    draw = ImageDraw.Draw(image)

    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]

    x = max((display.width - text_width) // 2, 0)
    y = max((display.height - text_height) // 2 - bbox[1], 0)  # ✅ ปรับให้ไม่ตกขอบล่าง

    draw.text((x, y), text, font=font, fill=255)
    with display_lock:
        display.image(image)
        display.show()

def reset_pipe(pipe_path="/tmp/keypad_pipe"):
    try:
        if os.path.exists(pipe_path):
            os.remove(pipe_path)
            print(f"🗑️ ลบ pipe เก่าแล้ว: {pipe_path}")
        os.mkfifo(pipe_path)
        print(f"✅ สร้าง pipe ใหม่เรียบร้อย: {pipe_path}")
    except Exception as e:
        print(f"❌ เกิดข้อผิดพลาดขณะ reset pipe: {e}")


# ใช้ raw mode เพื่อไม่ต้องกด Enter
def realtime_input():
    text = ""
    pipe_path = "/tmp/keypad_pipe"

    reset_pipe(pipe_path)

    print("⌨️ กดข้อความผ่าน Keypad (Ent เพื่อส่ง, ← เพื่อลบทีละตัว, Esc เพื่อล้าง):")

    first_input = True  # ✅ เพิ่ม flag ว่าเพิ่งเริ่มรับข้อความ

    # The keypad daemon opens the FIFO for one key and then closes it.  When
    # the writer closes, read() returns EOF; reopen the FIFO so the process
    # blocks for the next key instead of spinning at 100% CPU.
    while True:
        try:
            with open(pipe_path, "r", encoding="utf-8") as pipe:
                while True:
                    char = pipe.read(1)
                    if not char:
                        break

                    # ✅ เคลียร์จอเมื่อเริ่มข้อความใหม่
                    if first_input:
                        clear_display()
                        first_input = False

                    if char == "←":
                        text = text[:-1]
                    elif char == "E":
                        if pipe.read(2) == "nt":
                            draw_text("")  # เคลียร์จอ
                            return text
                    else:
                        text += char

                    draw_text(text)
        except FileNotFoundError:
            reset_pipe(pipe_path)
            time.sleep(0.1)


    print("\n👋 ออกจาก realtime input")
def realtime_input_check_special():
    pipe_path = "/tmp/keypad_pipe"
    reset_pipe(pipe_path)

    # Reopen after EOF so an idle process sleeps in open() instead of busy
    # looping after the keypad daemon closes its one-key writer.
    while True:
        try:
            with open(pipe_path, "r", encoding="utf-8") as pipe:
                while True:
                    char = pipe.read(1)
                    if not char:
                        break

                    if char == "E":
                        next_chars = pipe.read(2)
                        if next_chars == "sc":
                            return True
        except FileNotFoundError:
            reset_pipe(pipe_path)
            time.sleep(0.1)

def scroll_text_background(text, speed=OLED_FRAME_DELAY):
    start_scroll(text, speed)

def scroll_text_controlled(text, speed=OLED_FRAME_DELAY, stop_event=None):
    if stop_event is None:
        stop_event = scroll_stop_event

    thaifont = ImageFont.truetype("/usr/share/fonts/truetype/tlwg/Kinnari.ttf", 24)

    image = Image.new("1", (display.width, display.height))
    draw = ImageDraw.Draw(image)
    bbox = draw.textbbox((0, 0), text, font=thaifont)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    y = max((display.height - text_height) // 2 - bbox[1], 0)

    if stop_event.is_set():
        return

    if text_width <= display.width:
        # ถ้าข้อความสั้น แสดงอยู่ตรงกลางนิ่งๆ
        image = Image.new("1", (display.width, display.height))
        draw = ImageDraw.Draw(image)
        x = (display.width - text_width) // 2
        draw.text((x, y), text, font=thaifont, fill=255)
        with display_lock:
            display.image(image)
            display.show()
        return

    # ข้อความยาว → scroll จาก x=0 ไป x=-(text_width - display.width)
    start_x = 0
    end_x = -(text_width - display.width)

    while not stop_event.is_set():
        for offset in range(start_x, end_x - 1, -OLED_PIXEL_STEP):
            if stop_event.is_set():
                break
            image = Image.new("1", (display.width, display.height))
            draw = ImageDraw.Draw(image)
            draw.text((offset, y), text, font=thaifont, fill=255)
            with display_lock:
                display.image(image)
                display.show()
            stop_event.wait(speed)
        stop_event.wait(OLED_LOOP_PAUSE)



def start_scroll(text, speed=OLED_FRAME_DELAY):
    global scroll_thread, scroll_stop_event
    stop_scroll()
    if scroll_thread is not None and scroll_thread.is_alive():
        print("⚠️ OLED scroll เดิมยังหยุดไม่สนิท จึงไม่สร้าง thread เพิ่ม")
        return
    scroll_stop_event = threading.Event()
    thread = threading.Thread(
        target=scroll_text_controlled,
        args=(text, speed, scroll_stop_event),
        daemon=True,
    )
    scroll_thread = thread
    thread.start()

def stop_scroll():
    global scroll_thread
    scroll_stop_event.set()
    thread = scroll_thread
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=2.0)
    if thread is None or not thread.is_alive():
        scroll_thread = None

def clear_display():
    image = Image.new("1", (display.width, display.height))
    with display_lock:
        display.image(image)
        display.show()


#--------------------------------------------------------------------------------

#RGB--------------------------------------------------------------------------------

RED = 27
YELLOW = 17
GREEN = 22
# state สำหรับบอกให้หยุดกระพริบ
blinking = {"green": False, "yellow": False, "red": False}

def init_led():
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(RED, GPIO.OUT)
    GPIO.setup(YELLOW, GPIO.OUT)
    GPIO.setup(GREEN, GPIO.OUT)

def blink(pin, name):
    while blinking[name]:
        GPIO.output(pin, GPIO.HIGH)
        time.sleep(0.5)
        GPIO.output(pin, GPIO.LOW)
        time.sleep(0.5)

def start_blink(color):
    stop_all()  # หยุดทุกไฟก่อน
    blinking[color] = True
    if color == "green":
        thread = threading.Thread(target=blink, args=(GREEN, "green"))
    elif color == "yellow":
        thread = threading.Thread(target=blink, args=(YELLOW, "yellow"))
    elif color == "red":
        thread = threading.Thread(target=blink, args=(RED, "red"))
    thread.daemon = True  # ปิดโปรแกรมแล้ว thread จะหยุด
    thread.start()

def stop_all():
    for key in blinking:
        blinking[key] = False
    GPIO.output(RED, GPIO.LOW)
    GPIO.output(YELLOW, GPIO.LOW)
    GPIO.output(GREEN, GPIO.LOW)

def close():
    stop_all()
    GPIO.cleanup()

#--------------------------------------------------------------------------------

USB_IMG_PATH = "/home/tee/Desktop/dbindex/usb.img"
USB_MOUNT_PATH = "/mnt/usbimg" 
MOUNT_SETTLE_DELAY = 0.1
UNMOUNT_SETTLE_DELAY = 0.1
GADGET_READY_DELAY = 5.0
COMPLETION_DISPLAY_DELAY = 0.5
flash_path = None
def find_usb_flash_mount():
    attempts = 0
    while True:
        try:
            result = subprocess.run(["lsblk", "-o", "NAME,TRAN,MOUNTPOINT", "-J"], capture_output=True, text=True)
            data = json.loads(result.stdout)
            for dev in data["blockdevices"]:
                if dev.get("tran") == "usb" and "children" in dev:
                    for part in dev["children"]:
                        mountpoint = part.get("mountpoint")
                        if mountpoint and os.path.ismount(mountpoint):
                            return mountpoint
            attempts += 1
            if attempts > 10:
                raise RuntimeError("🔌 Flash Drive ไม่ตอบสนองเกิน 10 วินาที")
        except Exception as e:
            print(f"⚠️ Error: {e}")
        time.sleep(1)


#PATTERN_ROOT = find_usb_flash_mount()
PATTERN_ROOT = "/home/tee/Desktop/dbindex/rootfolder"
print(f"✅ พบ USB Flash Drive ที่: {PATTERN_ROOT}")

def list_pattern_folders():
    try:
        folders = [
            f for f in os.listdir(PATTERN_ROOT)
            if os.path.isdir(os.path.join(PATTERN_ROOT, f)) and not f.startswith('.')
        ]
        folders.sort()
        return folders
    except FileNotFoundError:
        print(f"❌ ไม่พบโฟลเดอร์: {PATTERN_ROOT}")
        return []


def mount_image():
    print("🔄 Mounting image...")
    uid = str(os.getuid())
    gid = str(os.getgid())

    # Unmount only this image.  Do not detach every loop device on the Pi.
    subprocess.run(["sudo", "umount", USB_MOUNT_PATH], stderr=subprocess.DEVNULL)
    time.sleep(MOUNT_SETTLE_DELAY)

    # ลอง mount
    subprocess.run([
        "sudo", "mount",
        "-o", f"loop,uid={uid},gid={gid}",
        USB_IMG_PATH,
        USB_MOUNT_PATH
    ], check=True)


def unmount_image():
    print("✅ Unmounting image...")
    subprocess.run(["sudo", "umount", USB_MOUNT_PATH], check=True)
    time.sleep(UNMOUNT_SETTLE_DELAY)

def copy_dst_files(folder_name):
    src_path = os.path.join(PATTERN_ROOT, folder_name)
    dst_path = USB_MOUNT_PATH

    # Keep unchanged files in place.  This is much faster for repeated slots
    # containing hundreds of small DST files and reduces SD-card writes.
    try:
        subprocess.run([
            "rsync",
            "-rt",
            "--whole-file",
            "--checksum",
            "--delete",
            "--delete-excluded",
            "--modify-window=2",
            "--no-perms",
            "--no-owner",
            "--no-group",
            "--omit-dir-times",
            "--exclude=.DS_Store",
            "--exclude=._*",
            f"{src_path}/",
            f"{dst_path}/",
        ], check=True)
    except (OSError, subprocess.CalledProcessError) as e:
        start_blink("red")
        stop_scroll()
        clear_display()
        start_scroll("ไฟล์เสีย")
        print(f"❌ Error syncing '{src_path}' → '{dst_path}': {e}")
        raise

    print(f"📁 คัดลอกโฟลเดอร์ '{folder_name}' → usb.img สำเร็จ")

def create_usb_image():
    if os.path.exists(USB_IMG_PATH):
        print("📦 ใช้ usb.img เดิม (ไม่ลบข้อมูลที่อาจค้างอยู่)")
        return

    print("🛠️  สร้าง usb.img ขนาด 64MB...")
    subprocess.run(["dd", "if=/dev/zero", f"of={USB_IMG_PATH}", "bs=1M", "count=64"], check=True)
    subprocess.run(["mkfs.vfat", USB_IMG_PATH], check=True)
    subprocess.run(["chmod", "+rw", USB_IMG_PATH], check=True)
    subprocess.run(["sync"])
    print("✅ usb.img พร้อมใช้งาน\n")


mount_lock_file = None

def acquire_mount_lock():
    global mount_lock_file
    mount_lock_file = open("/tmp/pistitchdrive-mount.lock", "w")
    try:
        fcntl.flock(mount_lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        mount_lock_file.close()
        mount_lock_file = None
        print("⚠️ mount.py กำลังทำงานอยู่แล้ว จึงไม่เปิดซ้ำ")
        return False
    return True

def release_mount_lock():
    global mount_lock_file
    if mount_lock_file is not None:
        fcntl.flock(mount_lock_file.fileno(), fcntl.LOCK_UN)
        mount_lock_file.close()
        mount_lock_file = None

def save_back_from_usb(folder_name):
    target_dir = os.path.join(PATTERN_ROOT, folder_name)
    rollback_root = os.path.join(os.path.dirname(PATTERN_ROOT), ".slot-backups")
    os.makedirs(rollback_root, exist_ok=True)
    previous_dir = os.path.join(rollback_root, folder_name)
    staging_dir = tempfile.mkdtemp(
        prefix=f".{folder_name}.incoming-",
        dir=PATTERN_ROOT,
    )
    image_mounted = False

    try:
        subprocess.run(["sudo", "modprobe", "-r", "g_mass_storage"], check=True)
        time.sleep(MOUNT_SETTLE_DELAY)

        mount_image()
        image_mounted = True
        print("📥 ดึงไฟล์กลับจาก usb.img...")

        # Copy to a staging directory first.  The current slot is untouched
        # until every file has been copied and verified.
        subprocess.run([
            "rsync",
            "-rt",
            "--whole-file",
            "--no-perms",
            "--no-owner",
            "--no-group",
            "--omit-dir-times",
            f"{USB_MOUNT_PATH}/",
            f"{staging_dir}/",
        ], check=True)

        verification = subprocess.run([
            "rsync",
            "-rcn",
            "--delete",
            "--itemize-changes",
            "--no-perms",
            "--no-owner",
            "--no-group",
            f"{USB_MOUNT_PATH}/",
            f"{staging_dir}/",
        ], check=True, capture_output=True, text=True)
        if verification.stdout.strip():
            raise RuntimeError("ไฟล์ staging ไม่ตรงกับ usb.img")

        unmount_image()
        image_mounted = False

        # Keep one recoverable previous version.  Renames are atomic because
        # staging, target, and previous all live on the same filesystem.
        if os.path.exists(previous_dir):
            shutil.rmtree(previous_dir)

        target_existed = os.path.exists(target_dir)
        if target_existed:
            os.replace(target_dir, previous_dir)

        try:
            os.replace(staging_dir, target_dir)
            staging_dir = None
        except Exception:
            if target_existed and os.path.exists(previous_dir) and not os.path.exists(target_dir):
                os.replace(previous_dir, target_dir)
            raise

        os.sync()
        print(f"✅ ดึงไฟล์กลับสำเร็จ และเก็บเวอร์ชันก่อนหน้าไว้ที่ {previous_dir}")
        return True
    except Exception as e:
        print(f"❌ ดึงไฟล์กลับล้มเหลว: {e}")
        return False
    finally:
        if image_mounted:
            try:
                unmount_image()
            except Exception as unmount_error:
                print(f"❌ Unmount หลังเกิดข้อผิดพลาดล้มเหลว: {unmount_error}")
        if staging_dir and os.path.exists(staging_dir):
            shutil.rmtree(staging_dir, ignore_errors=True)




def main():
    create_usb_image()
    print("📂 Available pattern folders:")
    init_led()
    start_blink("yellow")
    folders = list_pattern_folders()
    if not folders:
        return

    for i, name in enumerate(folders):
        print(f"  [{i}] {name}")
    start_blink("green")  # ไฟเขียวกระพริบ
    stop_scroll()
    clear_display()
    start_scroll("ใส่เลขช่อง")
    stop_scroll()
    idx = realtime_input()
    try:
        if idx != "0":
            selected = folders[int(idx)-1]
        else:
            raise ValueError("error")
        stop_scroll()
        clear_display()
        draw_text(idx)
        start_blink("yellow")
    except (IndexError, ValueError):
        print("❌ เลือกหมายเลขไม่ถูกต้อง")
        start_blink("red")
        stop_scroll()
        clear_display()
        start_scroll("ไม่มีช่อง "+str(idx))
        time.sleep(2)
        return

    mount_image()
    try:
        copy_dst_files(selected)
    finally:
        unmount_image()
    try:
        # ถอด g_mass_storage ออกก่อน (จะ error ถ้ายังไม่ได้โหลด ก็จับไว้)
        subprocess.run(["sudo", "modprobe", "-r", "g_mass_storage"], check=False)

        # โหลดใหม่พร้อมไฟล์ img
        subprocess.run([
            "sudo", "modprobe", "g_mass_storage",
            f"file={USB_IMG_PATH}",
            "stall=0",
            "removable=1",
            "ro=0"
        ], check=True)
        start_blink("green")
        stop_scroll()
        clear_display()
        start_scroll(str(idx)+" เสียบเครื่อง")
        time.sleep(GADGET_READY_DELAY)
        print("✅ g_mass_storage ถูกโหลดเรียบร้อย")
    except subprocess.CalledProcessError as e:
        start_blink("red")
        stop_scroll()
        clear_display()
        start_scroll("ผิดพลาดกด → เพื่อเริ่มใหม่")
        time.sleep(2)
        print("❌ เกิดข้อผิดพลาดในการโหลด g_mass_storage:", e)
    start_blink("yellow")
    stop_scroll()
    clear_display()
    start_scroll("เสร็จแล้วกด Esc")
    while True:
        if realtime_input_check_special():
            stop_scroll()
            clear_display()
            if save_back_from_usb(selected):
                start_scroll("เสร็จ")
            else:
                start_blink("red")
                start_scroll("บันทึกไม่สำเร็จ")
            time.sleep(COMPLETION_DISPLAY_DELAY)
            break

        

if __name__ == "__main__":
    if acquire_mount_lock():
        try:
            main()
        finally:
            stop_scroll()
            release_mount_lock()
