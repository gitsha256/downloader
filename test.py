import os
import asyncio
import aiohttp
import aiofiles
from tqdm import tqdm
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urlparse
import ttkbootstrap as ttk
from ttkbootstrap.constants import *
from PIL import Image, ImageTk
import threading
import time
import logging
import platform
import pystray
import random
import subprocess
import traceback

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)

# --- App State ---
app_state = {
    "download_queue": [],
    "completed_downloads": [],
    "is_processing": False,
    "is_paused": False,
    "stop_current_download": False,
    "current_download_speed": 0,
    "async_loop": None,
    "current_progress": {"size": "", "time_remaining": "", "speed": "", "percentage": ""}
}

# --- Utility Functions ---
def get_file_icon(path):
    ext = os.path.splitext(path)[1].lower()
    return {"mp4": "🎬", "zip": "📦", "mp3": "🎵", "pdf": "📄"}.get(ext[1:], "📁")

def play_complete_sound():
    if platform.system() == "Windows":
        import winsound
        winsound.MessageBeep()
    else:
        print("\a")

def format_size(bytes_size):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_size < 1024:
            return f"{bytes_size:.2f} {unit}"
        bytes_size /= 1024
    return f"{bytes_size:.2f} PB"

def format_time(seconds):
    if seconds is None:
        return "Unknown"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def open_folder(path):
    folder = os.path.dirname(path)
    if platform.system() == "Windows":
        os.startfile(folder)
    elif platform.system() == "Darwin":
        subprocess.run(["open", folder])
    else:
        subprocess.run(["xdg-open", folder])

def create_custom_dialog(title, message, detail, output_file):
    dialog = tk.Toplevel()
    dialog.title(title)
    dialog.geometry("400x250")
    dialog.resizable(False, False)
    dialog.transient(dialog.master)
    dialog.grab_set()

    ttk.Label(dialog, text=message, font=("Segoe UI", 12, "bold")).pack(pady=15)
    ttk.Label(dialog, text=detail, font=("Segoe UI", 10), wraplength=300).pack(pady=10)

    button_frame = ttk.Frame(dialog)
    button_frame.pack(pady=15, fill="x", expand=True)
    ttk.Button(button_frame, text="OK", command=dialog.destroy, width=10).pack(side="left", padx=10)
    ttk.Button(button_frame, text="Open Folder", command=lambda: [open_folder(output_file), dialog.destroy()], width=12).pack(side="left", padx=10)

    dialog.update_idletasks()
    x = dialog.master.winfo_rootx() + (dialog.master.winfo_width() - dialog.winfo_width()) // 2
    y = dialog.master.winfo_rooty() + (dialog.master.winfo_height() - dialog.winfo_height()) // 2
    dialog.geometry(f"+{x}+{y}")
    dialog.wait_window()

async def resolve_filename_from_url(url):
    filename = os.path.basename(urlparse(url).path)
    return filename if filename else "downloaded_file"

def update_gui_progress(pbar, gui):
    percentage = 0
    if pbar.total and pbar.total > 1024:
        size_str = f"{format_size(pbar.n)}/{format_size(pbar.total)}"
        speed = pbar.format_dict.get('rate', 0) or 0
        speed_str = f"{format_size(speed)}/s" if speed else "0 B/s"
        elapsed = pbar.format_dict.get('elapsed', 0)
        remaining = (pbar.total - pbar.n) / speed if speed and pbar.total else None
        time_remaining = format_time(remaining)
        percentage = (pbar.n / pbar.total) * 100
        if pbar.n > pbar.total:
            percentage = 100.0
            size_str = f"{format_size(pbar.n)}/{format_size(pbar.total)} (overshot)"
            logging.warning(f"Download overshot expected size: {pbar.n} > {pbar.total}")
    else:
        size_str = f"{format_size(pbar.n)}/Unknown"
        speed = pbar.format_dict.get('rate', 0) or 0
        speed_str = f"{format_size(speed)}/s" if speed else "0 B/s"
        elapsed = pbar.format_dict.get('elapsed', 0)
        time_remaining = "Unknown"
        percentage = min(pbar.n / (1024 * 1024 * 100) * 100, 100)

    app_state["current_progress"] = {
        "size": size_str,
        "time_remaining": time_remaining,
        "speed": speed_str,
        "percentage": f"{percentage:.2f}%"
    }
    logging.debug(f"Updating GUI: size={size_str}, time={time_remaining}, speed={speed_str}, percentage={percentage:.2f}%")
    gui['root'].after(0, lambda: gui['size_label'].config(text=f"Size: {size_str}"))
    gui['root'].after(0, lambda: gui['time_label'].config(text=f"Time: {time_remaining}"))
    gui['root'].after(0, lambda: gui['speed_label'].config(text=f"Speed: {speed_str}"))
    gui['root'].after(0, lambda: gui['percent_label'].config(text=f"Progress: {percentage:.2f}%"))

# --- Single Stream Download ---
async def single_stream_download(session, url, output_file, pbar, lock, progress_var, gui, retries=5, backoff=2):
    user_agents = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36'
    ]
    headers = {
        'User-Agent': random.choice(user_agents),
        'Accept': '*/*',
        'Accept-Encoding': 'identity',
        'Referer': urlparse(url).scheme + '://' + urlparse(url).netloc,
        'Connection': 'keep-alive',
        'Keep-Alive': 'timeout=600, max=1000',
        'Pragma': 'no-cache'
    }
    downloaded = 0
    if os.path.exists(output_file):
        downloaded = os.path.getsize(output_file)
        headers['Range'] = f'bytes={downloaded}-'

    start_time = time.time()
    for attempt in range(retries):
        try:
            logging.info(f"Starting single-stream connection for {url}, attempt {attempt+1}/{retries}")
            await asyncio.sleep(random.uniform(0.1, 0.3))
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=360)) as r:
                if r.status == 416:
                    logging.warning(f"Range request failed for {url}. Attempting full download.")
                    headers.pop('Range', None)
                    downloaded = 0
                    async with aiofiles.open(output_file, 'wb') as f:
                        pass
                    continue

                r.raise_for_status()
                total_size = int(r.headers.get("Content-Length", 0)) + downloaded if r.status == 206 else int(r.headers.get("Content-Length", 0))

                if downloaded > 0 and total_size > 0 and downloaded >= total_size:
                    logging.warning(f"Local file size ({downloaded}) exceeds server size ({total_size}). Starting full download.")
                    headers.pop('Range', None)
                    downloaded = 0
                    async with aiofiles.open(output_file, 'wb') as f:
                        pass
                    continue

                pbar.total = total_size if total_size > 1024 else None
                logging.info(f"Response headers: {r.headers}")
                async with aiofiles.open(output_file, 'ab' if downloaded > 0 else 'wb') as f:
                    async for chunk in r.content.iter_chunked(131072):
                        if app_state["is_paused"]:
                            await asyncio.sleep(0.5)
                        if app_state["stop_current_download"]:
                            logging.info(f"Download stopped for {url}")
                            return False
                        await f.write(chunk)
                        async with lock:
                            downloaded += len(chunk)
                            pbar.update(len(chunk))
                            update_gui_progress(pbar, gui)
                            if pbar.total and pbar.total > 1024:
                                progress_var.set((pbar.n / pbar.total) * 100)
                            else:
                                progress_var.set(min(downloaded / (1024 * 1024 * 100) * 100, 100))
                final_size = os.path.getsize(output_file)
                if total_size == 0 or (total_size and (abs(final_size - total_size) <= max(total_size * 0.01, 1024 * 1024) or final_size >= total_size)):
                    single_stream_speed = downloaded / (time.time() - start_time) if (time.time() - start_time) > 0 else 0
                    logging.info(f"Single stream download completed for {url}, speed: {format_size(single_stream_speed)}/s, final_size: {format_size(final_size)}, expected: {format_size(total_size) if total_size else 'Unknown'}")
                    return True
                else:
                    raise ValueError(f"Incomplete or overshot download: expected {format_size(total_size)}, got {format_size(final_size)}")
        except aiohttp.ClientResponseError as e:
            logging.warning(f"Single stream attempt {attempt+1}/{retries} failed for {url}: {e.status}, {e.message}")
            if attempt < retries - 1:
                await asyncio.sleep((2 ** attempt) + random.uniform(0.1, 1.0))
                headers['Range'] = f'bytes={os.path.getsize(output_file) if os.path.exists(output_file) else 0}-'
                headers['User-Agent'] = random.choice(user_agents)
            else:
                logging.error(f"Single stream download failed for {url} after {retries} attempts: {e}\n{traceback.format_exc()}")
                gui['root'].after(0, lambda err=str(e): messagebox.showerror("Download Failed", f"Failed to download {url}: {err}\nCheck your network connection or try a different server."))
                return False
        except Exception as e:
            if app_state["stop_current_download"]:
                logging.info(f"Download stopped for {url}")
                return False
            logging.warning(f"Single stream attempt {attempt+1}/{retries} failed for {url}: {str(e)}")
            if attempt < retries - 1:
                await asyncio.sleep((2 ** attempt) + random.uniform(0.1, 1.0))
                headers['Range'] = f'bytes={os.path.getsize(output_file) if os.path.exists(output_file) else 0}-'
                headers['User-Agent'] = random.choice(user_agents)
            else:
                logging.error(f"Single stream download failed for {url} after {retries} attempts: {str(e)}\n{traceback.format_exc()}")
                gui['root'].after(0, lambda err=str(e): messagebox.showerror("Download Failed", f"Failed to download {url}: {err}\nCheck your network connection or try a different server."))
                return False
    return False

# --- Download Manager ---
async def download_manager_async(url, output_file, gui):
    app_state["stop_current_download"] = False
    lock = asyncio.Lock()
    try:
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=1, force_close=False),
            cookie_jar=aiohttp.CookieJar()
        ) as session:
            logging.info(f"Starting single-stream download for {url}")
            with tqdm(total=None, unit='B', unit_scale=True, desc="Downloading") as pbar:
                success = await single_stream_download(session, url, output_file, pbar, lock, gui['progress_var'], gui)
            if success:
                final_size = os.path.getsize(output_file)
                app_state["completed_downloads"].append({"filename": os.path.basename(output_file), "path": output_file})
                gui['root'].after(0, lambda: gui['completed_listbox'].insert(tk.END, f"{get_file_icon(output_file)} {os.path.basename(output_file)}"))
                gui['root'].after(0, lambda: create_custom_dialog(
                    "Download Successful",
                    f"Downloaded: {os.path.basename(output_file)}",
                    f"Click OK to continue or Open Folder to view the file.\nFinal size: {format_size(final_size)}",
                    output_file
                ))
                play_complete_sound()
                logging.info(f"Download completed for {url}, final_size: {format_size(final_size)}")
    except Exception as e:
        if not app_state["stop_current_download"]:
            logging.error(f"Download manager error for {url}: {str(e)}\n{traceback.format_exc()}")
        gui['root'].after(0, lambda err=str(e): messagebox.showerror("Download Failed", f"Failed to download {url}: {err}\nCheck your network connection or try a different server."))

# --- Queue Processing, Threading, GUI, System Tray ---
async def process_queue_async(gui, selected_index=None):
    if selected_index is not None:
        if 0 <= selected_index < len(app_state["download_queue"]):
            job = app_state["download_queue"][selected_index]
            url, output_file = job['url'], job['output_file']
            gui['root'].after(0, gui['current_file_label'].config, {"text": f"Downloading: {os.path.basename(output_file)}"})
            await download_manager_async(url, output_file, gui)
            if not app_state["stop_current_download"]:
                app_state["download_queue"].pop(selected_index)
                gui['root'].after(0, gui['queue_listbox'].delete, selected_index)
        else:
            gui['root'].after(0, lambda: messagebox.showwarning("Invalid Selection", "Selected item is no longer in the queue."))
    else:
        while app_state["download_queue"]:
            job = app_state["download_queue"][0]
            url, output_file = job['url'], job['output_file']
            gui['root'].after(0, gui['current_file_label'].config, {"text": f"Downloading: {os.path.basename(output_file)}"})
            await download_manager_async(url, output_file, gui)
            if not app_state["stop_current_download"]:
                app_state["download_queue"].pop(0)
                gui['root'].after(0, gui['queue_listbox'].delete, 0)
            else:
                break
    app_state["is_processing"] = False
    gui['root'].after(0, update_button_states, gui)
    gui['root'].after(0, gui['current_file_label'].config, {"text": "Idle"})
    gui['root'].after(0, lambda: gui['size_label'].config(text="Size: N/A"))
    gui['root'].after(0, lambda: gui['time_label'].config(text="Time: N/A"))
    gui['root'].after(0, lambda: gui['speed_label'].config(text="Speed: 0.00 MB/s"))
    gui['root'].after(0, lambda: gui['percent_label'].config(text="Progress: 0.00%"))

def start_asyncio_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app_state["async_loop"] = loop
    loop.run_forever()

def run_async_task(coro):
    if app_state["async_loop"]:
        asyncio.run_coroutine_threadsafe(coro, app_state["async_loop"])

def update_speed_label(label):
    speeds = []
    while True:
        time.sleep(1)
        bps = app_state["current_download_speed"]
        speeds.append(bps)
        if len(speeds) > 5:
            speeds.pop(0)
        app_state["current_download_speed"] = 0

def add_to_queue(entry, listbox):
    url = entry.get().strip()
    if not url:
        messagebox.showwarning("Missing URL", "Please enter a valid URL.")
        return
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        filename = loop.run_until_complete(resolve_filename_from_url(url))
        loop.close()
    except Exception as e:
        messagebox.showerror("Error", f"Failed to resolve URL: {str(e)}\nCheck your network connection or try a different server.")
        logging.error(f"URL resolution failed: {str(e)}\n{traceback.format_exc()}")
        return

    save_path = filedialog.asksaveasfilename(initialfile=filename)
    if not save_path:
        messagebox.showinfo("Cancelled", "No save location selected.")
        return

    job = {"url": url, "output_file": save_path}
    app_state["download_queue"].append(job)
    listbox.insert(tk.END, f"{get_file_icon(save_path)} {os.path.basename(save_path)}")
    entry.delete(0, tk.END)
    logging.info(f"Added to queue: {url} -> {save_path}")

def remove_from_queue(listbox):
    sel = listbox.curselection()
    if sel:
        idx = sel[0]
        listbox.delete(idx)
        del app_state["download_queue"][idx]
        logging.info(f"Removed item from queue at index {idx}")
    else:
        messagebox.showwarning("No Selection", "Please select an item to remove from the queue.")

def open_completed_folder(completed_listbox):
    sel = completed_listbox.curselection()
    if sel:
        idx = sel[0]
        path = app_state["completed_downloads"][idx]["path"]
        open_folder(path)
    else:
        messagebox.showwarning("No Selection", "Please select a completed download to open its folder.")

def start_queue(gui):
    if not app_state["download_queue"]:
        messagebox.showinfo("Empty Queue", "The download queue is empty. Add a URL to start downloading.")
        return
    if not app_state["is_processing"]:
        app_state["is_processing"] = True
        app_state["is_paused"] = False
        download_mode = gui['download_mode'].get()
        if download_mode == "Download Selected":
            sel = gui['queue_listbox'].curselection()
            if not sel:
                messagebox.showwarning("No Selection", "Please select an item to download.")
                app_state["is_processing"] = False
                return
            selected_index = sel[0]
            run_async_task(process_queue_async(gui, selected_index=selected_index))
        else:
            run_async_task(process_queue_async(gui))
        update_button_states(gui)

def pause_resume(gui):
    app_state["is_paused"] = not app_state["is_paused"]
    update_button_states(gui)

def stop_download(gui):
    app_state["stop_current_download"] = True
    app_state["is_processing"] = False
    update_button_states(gui)
    gui['root'].after(0, lambda: gui['size_label'].config(text="Size: N/A"))
    gui['root'].after(0, lambda: gui['time_label'].config(text="Time: N/A"))
    gui['root'].after(0, lambda: gui['speed_label'].config(text="Speed: 0.00 MB/s"))
    gui['root'].after(0, lambda: gui['percent_label'].config(text="Progress: 0.00%"))

def update_button_states(gui):
    processing = app_state["is_processing"]
    gui['start_button'].config(state='disabled' if processing else 'normal')
    gui['stop_button'].config(state='normal' if processing else 'disabled')
    gui['pause_resume_button'].config(
        state='normal' if processing else 'disabled',
        text="Pause" if not app_state["is_paused"] else "Resume"
    )
    gui['tray_button'].config(state='normal')

def quit_app(icon, root):
    icon.stop()
    root.quit()
    root.destroy()
    os._exit(0)

def show_window(icon, root):
    icon.stop()
    root.deiconify()

def minimize_to_tray(root):
    root.withdraw()
    tray_icon_path = "icon.ico"
    try:
        image = Image.open(tray_icon_path)
    except:
        image = Image.new('RGB', (64, 64), color='skyblue')
    icon = pystray.Icon(
        "downloader", image, "Async Downloader",
        menu=pystray.Menu(
            pystray.MenuItem("Show", lambda: show_window(icon, root), default=True),
            pystray.MenuItem("Quit", lambda: quit_app(icon, root))
        )
    )
    threading.Thread(target=icon.run, daemon=True).start()

def main():
    root = ttk.Window(themename="darkly")
    root.title("PYDownloader")
    root.geometry("600x600")

    icons = {}
    for name in ['add.png', 'start.png', 'pause.png', 'stop.png', 'remove.png','download.png']:
        try:
            icons[name] = ImageTk.PhotoImage(Image.open(name).resize((20, 20)))
        except:
            icons[name] = None

    url_frame = ttk.Frame(root)
    url_frame.pack(fill="x", padx=10, pady=5)
    url_entry = ttk.Entry(url_frame)
    url_entry.pack(side="left", fill="x", expand=True)
    url_entry.bind("<Return>", lambda event: add_to_queue(url_entry, queue_listbox))

    queue_frame = ttk.LabelFrame(root, text="Download Queue", padding=5)
    queue_frame.pack(fill="both", expand=True, padx=10, pady=5)
    queue_listbox = tk.Listbox(queue_frame, font=("Segoe UI", 10), height=5)
    queue_listbox.pack(fill="both", expand=True)

    completed_frame = ttk.LabelFrame(root, text="Completed Downloads", padding=5)
    completed_frame.pack(fill="both", expand=True, padx=10, pady=5)
    completed_listbox = tk.Listbox(completed_frame, font=("Segoe UI", 10), height=5)
    completed_listbox.pack(fill="both", expand=True)

    queue_buttons_frame = ttk.Frame(root)
    queue_buttons_frame.pack(fill="x", padx=10, pady=5)
    add_button = ttk.Button(
        queue_buttons_frame, text="Add to Queue", image=icons.get('add.png'), compound="left" if icons.get('add.png') else None,
        command=lambda: add_to_queue(url_entry, queue_listbox)
    )
    add_button.pack(side="left", expand=True, fill="x", padx=2)
    remove_button = ttk.Button(
        queue_buttons_frame, text="Remove", image=icons.get('remove.png'), compound="left" if icons.get('remove.png') else None,
        command=lambda: remove_from_queue(queue_listbox)
    )
    remove_button.pack(side="left", padx=2)
    open_completed_button = ttk.Button(
        queue_buttons_frame, text="Open Folder", image=icons.get('start.png'), compound="left" if icons.get('start.png') else None,
        command=lambda: open_completed_folder(completed_listbox)
    )
    open_completed_button.pack(side="left", padx=2)
    start_btn = ttk.Button(
        queue_buttons_frame, text="Start", image=icons.get('download.png'), compound="left" if icons.get('download.png') else None
    )
    start_btn.pack(side="left", expand=True, fill="x", padx=2)

    download_mode_var = tk.StringVar(value="Download All")
    download_mode = ttk.Combobox(
        root, textvariable=download_mode_var, values=["Download All", "Download Selected"], state="readonly"
    )
    download_mode.pack(fill="x", padx=10, pady=5)

    progress_var = tk.DoubleVar()
    progress_bar = ttk.Progressbar(root, variable=progress_var, style="success.Striped.Horizontal.TProgressbar")
    progress_bar.pack(fill="x", padx=10)

    status = ttk.Label(root, text="Idle")
    status.pack(anchor="w", padx=10)

    info_frame = ttk.Frame(root)
    info_frame.pack(fill="x", padx=10, pady=5)
    size_label = ttk.Label(info_frame, text="Size: N/A")
    size_label.pack(side="left", padx=5)
    percent_label = ttk.Label(info_frame, text="Progress: 0.00%")
    percent_label.pack(side="left", padx=5)
    time_label = ttk.Label(info_frame, text="Time: N/A")
    time_label.pack(side="left", padx=5)
    speed_label = ttk.Label(info_frame, text="Speed: 0.00 MB/s")
    speed_label.pack(side="left", padx=5)

    control_frame = ttk.Frame(root)
    control_frame.pack(fill="x", pady=10, padx=10)
    pause_btn = ttk.Button(
        control_frame, text="Pause", image=icons.get('pause.png'), compound="left" if icons.get('pause.png') else None, state="disabled"
    )
    stop_btn = ttk.Button(
        control_frame, text="Stop", image=icons.get('stop.png'), compound="left" if icons.get('stop.png') else None, state="disabled"
    )
    tray_btn = ttk.Button(
        control_frame, text="Tray", image=None, width=10, command=lambda: minimize_to_tray(root)
    )

    pause_btn.pack(side="left", expand=True, fill="x", padx=2)
    stop_btn.pack(side="left", expand=True, fill="x", padx=2)
    tray_btn.pack(side="left", padx=2)

    start_btn.config(command=lambda: start_queue(gui_controls))
    pause_btn.config(command=lambda: pause_resume(gui_controls))
    stop_btn.config(command=lambda: stop_download(gui_controls))

    gui_controls = {
        "root": root,
        "queue_listbox": queue_listbox,
        "completed_listbox": completed_listbox,
        "current_file_label": status,
        "progress_var": progress_var,
        "start_button": start_btn,
        "pause_resume_button": pause_btn,
        "stop_button": stop_btn,
        "tray_button": tray_btn,
        "size_label": size_label,
        "time_label": time_label,
        "speed_label": speed_label,
        "percent_label": percent_label,
        "download_mode": download_mode_var
    }

    threading.Thread(target=start_asyncio_loop, daemon=True).start()
    threading.Thread(target=update_speed_label, args=(speed_label,), daemon=True).start()

    def on_close():
        if messagebox.askokcancel("Exit", "Close the downloader?"):
            root.destroy()
            os._exit(0)
    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()

if __name__ == "__main__":
    main()