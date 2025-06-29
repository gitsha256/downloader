import os
from urllib.parse import urlparse
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
import sys
import io

# --- Configuration ---
# A more conservative default to be polite to servers.
MIN_SIZE_FOR_MULTI_STREAM_MB = 5

# --- Logging ---
log_file = 'pydownloader.log'
is_windowed = sys.stderr is None
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_file) if is_windowed else logging.StreamHandler()
    ]
)

# --- App State ---
app_state = {
    "download_queue": [],
    "completed_downloads": [],
    "is_processing": False,
    "is_paused": False,
    "stop_current_download": False,
    "async_loop": None,
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
        print("\a") # Beep for macOS/Linux

def format_size(bytes_size):
    if bytes_size is None: return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_size < 1024:
            return f"{bytes_size:.2f} {unit}"
        bytes_size /= 1024
    return f"{bytes_size:.2f} PB"

def format_time(seconds):
    if seconds is None or seconds == float('inf') or seconds < 0:
        return "Unknown"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def open_folder(path):
    folder = os.path.dirname(path)
    try:
        if platform.system() == "Windows":
            os.startfile(folder)
        elif platform.system() == "Darwin": # macOS
            subprocess.run(["open", folder])
        else: # Linux
            subprocess.run(["xdg-open", folder])
    except Exception as e:
        logging.error(f"Could not open folder {folder}: {e}")
        messagebox.showerror("Error", f"Could not open folder:\n{folder}")

def create_custom_dialog(title, message, detail, output_file):
    dialog = tk.Toplevel()
    dialog.title(title)
    dialog.geometry("400x250")
    dialog.resizable(False, False)
    dialog.transient(dialog.master)
    dialog.grab_set()

    ttk.Label(dialog, text=message, font=("Segoe UI", 12, "bold")).pack(pady=15)
    ttk.Label(dialog, text=detail, font=("Segoe UI", 10), wraplength=380).pack(pady=10)

    button_frame = ttk.Frame(dialog)
    button_frame.pack(pady=15, fill="x", expand=True)
    ttk.Button(button_frame, text="OK", style="success.TButton", command=dialog.destroy, width=10).pack(side="left", padx=10, expand=True)
    ttk.Button(button_frame, text="Open Folder", style="info.TButton", command=lambda: [open_folder(output_file), dialog.destroy()], width=12).pack(side="left", padx=10, expand=True)

    dialog.update_idletasks()
    x = dialog.master.winfo_rootx() + (dialog.master.winfo_width() - dialog.winfo_width()) // 2
    y = dialog.master.winfo_rooty() + (dialog.master.winfo_height() - dialog.winfo_height()) // 2
    dialog.geometry(f"+{x}+{y}")
    dialog.wait_window()

async def resolve_filename_from_url(url):
    filename = os.path.basename(urlparse(url).path)
    return filename if filename else "downloaded_file"

def update_gui_progress(pbar, gui):
    if pbar is None: return
    percentage = 0
    if pbar.total and pbar.total > 0:
        size_str = f"{format_size(pbar.n)}/{format_size(pbar.total)}"
        speed = pbar.format_dict.get('rate', 0) or 0
        speed_str = f"{format_size(speed)}/s"
        remaining = (pbar.total - pbar.n) / speed if speed else float('inf')
        time_remaining = format_time(remaining)
        percentage = min((pbar.n / pbar.total) * 100, 100)
    else:
        size_str = f"{format_size(pbar.n)}/Unknown"
        speed = pbar.format_dict.get('rate', 0) or 0
        speed_str = f"{format_size(speed)}/s"
        time_remaining = "Unknown"
        percentage = 0

    gui['root'].after(0, lambda: gui['size_label'].config(text=f"Size: {size_str}"))
    gui['root'].after(0, lambda: gui['time_label'].config(text=f"Time: {time_remaining}"))
    gui['root'].after(0, lambda: gui['speed_label'].config(text=f"Speed: {speed_str}"))
    gui['root'].after(0, lambda: gui['percent_label'].config(text=f"Progress: {percentage:.2f}%"))
    gui['root'].after(0, lambda: gui['progress_var'].set(percentage))

# --- Core Download Logic ---

async def check_server_support(session, url):
    headers = {'Range': 'bytes=0-0', 'User-Agent': 'Mozilla/5.0'}
    try:
        async with session.get(url, headers=headers, timeout=15) as r:
            if r.status == 206:
                content_range = r.headers.get('Content-Range')
                if content_range:
                    try:
                        total_size = int(content_range.split('/')[-1])
                        logging.info(f"Server supports multi-stream. Total size: {format_size(total_size)}")
                        return True, total_size
                    except (ValueError, IndexError):
                        return False, 0
            elif r.status == 200:
                total_size = int(r.headers.get('Content-Length', 0))
                logging.info(f"Server sent full file on range request. Falling back. Size: {format_size(total_size)}")
                return False, total_size
            else:
                return False, 0
    except Exception as e:
        logging.error(f"Pre-flight check failed: {e}. Defaulting to single-stream.")
        return False, 0

async def download_chunk(session, url, output_file, start_byte, end_byte, pbar, lock, gui, retries=3):
    headers = {'Range': f'bytes={start_byte}-{end_byte}', 'User-Agent': 'Mozilla/5.0'}
    for attempt in range(retries):
        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=360)) as r:
                r.raise_for_status()
                async with aiofiles.open(output_file, 'r+b') as f:
                    await f.seek(start_byte)
                    downloaded_in_chunk = 0
                    async for chunk in r.content.iter_chunked(131072):
                        if app_state["stop_current_download"]: return False
                        while app_state["is_paused"]: await asyncio.sleep(0.5)
                        await f.write(chunk)
                        chunk_len = len(chunk)
                        downloaded_in_chunk += chunk_len
                        async with lock:
                            pbar.update(chunk_len)
                            update_gui_progress(pbar, gui)
                if downloaded_in_chunk >= (end_byte - start_byte + 1):
                    return True
                else:
                    logging.warning(f"Chunk {start_byte}-{end_byte} incomplete. Retrying.")
                    continue
        except aiohttp.ClientResponseError as e:
            if app_state["stop_current_download"]: return False
            if e.status in [502, 503, 504]:
                wait_time = 5 + (2 ** attempt)
                logging.warning(f"Chunk {start_byte}-{end_byte} failed with server error {e.status}. Waiting {wait_time}s.")
                await asyncio.sleep(wait_time)
            else:
                logging.error(f"Chunk {start_byte}-{end_byte} failed with unrecoverable client error {e.status}.")
                return False
        except Exception as e:
            if app_state["stop_current_download"]: return False
            logging.error(f"Chunk {start_byte}-{end_byte} failed on attempt {attempt+1}/{retries}: {e}")
            if attempt < retries - 1:
                await asyncio.sleep(2 ** attempt)
            else:
                return False
    return False

async def multi_stream_download(session, url, output_file, total_size, pbar, lock, gui, num_connections):
    logging.info(f"Starting multi-stream download for {url} with {num_connections} connections.")
    try:
        async with aiofiles.open(output_file, 'wb') as f:
            await f.truncate(total_size)
    except Exception as e:
        logging.error(f"Failed to pre-allocate file: {e}")
        return False

    chunk_size = total_size // num_connections
    tasks = []
    for i in range(num_connections):
        start = i * chunk_size
        end = start + chunk_size - 1
        if i == num_connections - 1: end = total_size - 1
        await asyncio.sleep(random.uniform(0.1, 0.4))
        if app_state["stop_current_download"]: return False
        tasks.append(download_chunk(session, url, output_file, start, end, pbar, lock, gui))

    results = await asyncio.gather(*tasks)
    return all(results)

async def single_stream_download(session, url, output_file, pbar, lock, gui, retries=5):
    logging.info(f"Starting single-stream download for {url}")
    headers = {'User-Agent': 'Mozilla/5.0'}
    downloaded = 0
    if os.path.exists(output_file):
        downloaded = os.path.getsize(output_file)
        headers['Range'] = f'bytes={downloaded}-'
        pbar.n = downloaded

    for attempt in range(retries):
        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=None)) as r:
                if r.status == 416:
                    logging.info(f"File already complete: {output_file}")
                    pbar.total = downloaded
                    pbar.n = downloaded
                    update_gui_progress(pbar, gui)
                    return True
                r.raise_for_status()
                content_length = r.headers.get("Content-Length")
                if content_length:
                    pbar.total = int(content_length) + (downloaded if r.status == 206 else 0)
                
                async with aiofiles.open(output_file, 'ab' if downloaded > 0 else 'wb') as f:
                    async for chunk in r.content.iter_chunked(131072):
                        if app_state["stop_current_download"]: return False
                        while app_state["is_paused"]: await asyncio.sleep(0.5)
                        await f.write(chunk)
                        async with lock:
                            pbar.update(len(chunk))
                            update_gui_progress(pbar, gui)
                return True
        except Exception as e:
            if app_state["stop_current_download"]: return False
            logging.error(f"Single stream attempt {attempt+1}/{retries} failed: {e}")
            if attempt < retries - 1:
                await asyncio.sleep(2 ** attempt)
            else:
                return False
    return False

async def download_manager_async(url, output_file, gui):
    app_state["stop_current_download"] = False
    lock = asyncio.Lock()
    success = False

    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as session:
            with tqdm(total=None, unit='B', unit_scale=True, desc="Downloading", file=io.StringIO()) as pbar:
                supports_multi, total_size = await check_server_support(session, url)

                if supports_multi and total_size > (MIN_SIZE_FOR_MULTI_STREAM_MB * 1024 * 1024):
                    pbar.total = total_size
                    hostname = urlparse(url).hostname or ""
                    if "seedr.cc" in hostname:
                        connection_attempts = [2]  # Only allow 2 for seedr
                    else:
                        connection_attempts = sorted(set([min(8, total_size // (10* 1024 * 1024)), 4, 2]), reverse=True)


                    for conn_count in connection_attempts:
                        if app_state["stop_current_download"]: break
                        pbar.n = 0
                        update_gui_progress(pbar, gui)
                        multi_success = await multi_stream_download(session, url, output_file, total_size, pbar, lock, gui, conn_count)
                        if multi_success:
                            success = True
                            logging.info(f"Multi-stream download succeeded with {conn_count} connections.")
                            break
                        else:
                            logging.warning(f"Multi-stream with {conn_count} connections failed. Trying with fewer.")
                            await asyncio.sleep(3)
                    
                    if not success and not app_state["stop_current_download"]:
                        logging.warning("All multi-stream attempts failed. Falling back to single-stream.")
                        if os.path.exists(output_file): os.remove(output_file)
                        pbar.n = 0
                        success = await single_stream_download(session, url, output_file, pbar, lock, gui)
                else:
                    success = await single_stream_download(session, url, output_file, pbar, lock, gui)

        if success and not app_state["stop_current_download"]:
            final_size = os.path.getsize(output_file)
            app_state["completed_downloads"].append({"filename": os.path.basename(output_file), "path": output_file})
            gui['root'].after(0, lambda: gui['completed_listbox'].insert(tk.END, f"{get_file_icon(output_file)} {os.path.basename(output_file)}"))
            gui['root'].after(0, lambda: create_custom_dialog("Download Successful", f"Downloaded: {os.path.basename(output_file)}", f"Final size: {format_size(final_size)}", output_file))
            play_complete_sound()
            logging.info(f"Download manager completed for {url}, final_size: {format_size(final_size)}")
        elif not app_state["stop_current_download"]:
            raise Exception("Download failed after all strategies.")

    except Exception as e:
        if not app_state["stop_current_download"]:
            logging.error(f"Download manager error for {url}: {str(e)}\n{traceback.format_exc()}")
            gui['root'].after(0, lambda: messagebox.showerror("Download Failed", f"Failed to download {url}: {e}"))
        if os.path.exists(output_file) and not success:
            logging.warning(f"A partial file may be left at {output_file}")

# --- App Logic & GUI ---

async def process_queue_async(gui, selected_index=None):
    # This logic now correctly handles downloading one or all items
    items_to_process = []
    if selected_index is not None:
        if 0 <= selected_index < len(app_state["download_queue"]):
            items_to_process.append((selected_index, app_state["download_queue"][selected_index]))
    else:
        # Create a list of all items to process so pops don't affect the loop
        items_to_process = list(enumerate(app_state["download_queue"]))[::-1] # Reverse to pop correctly

    for index, job in items_to_process:
        if app_state["stop_current_download"]:
            logging.info("Queue processing stopped by user.")
            break
        
        url, output_file = job['url'], job['output_file']
        gui['root'].after(0, gui['current_file_label'].config, {"text": f"Downloading: {os.path.basename(output_file)}"})
        await download_manager_async(url, output_file, gui)
        
        if not app_state["stop_current_download"]:
            # Find the actual index in the current queue before deleting
            try:
                current_idx = app_state["download_queue"].index(job)
                app_state["download_queue"].pop(current_idx)
                gui['root'].after(0, gui['queue_listbox'].delete, current_idx)
            except ValueError:
                logging.warning("Tried to remove an item that was already processed.")
    
    app_state["is_processing"] = False
    gui['root'].after(0, update_button_states, gui)
    gui['root'].after(0, gui['current_file_label'].config, {"text": "Idle"})
    gui['root'].after(0, lambda: gui['size_label'].config(text="Size: N/A"))
    gui['root'].after(0, lambda: gui['time_label'].config(text="Time: N/A"))
    gui['root'].after(0, lambda: gui['speed_label'].config(text="Speed: N/A"))
    gui['root'].after(0, lambda: gui['percent_label'].config(text="Progress: 0.00%"))
    gui['root'].after(0, lambda: gui['progress_var'].set(0))

def start_asyncio_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app_state["async_loop"] = loop
    loop.run_forever()

def run_async_task(coro):
    if app_state["async_loop"]:
        asyncio.run_coroutine_threadsafe(coro, app_state["async_loop"])

def add_to_queue(entry, listbox, placeholder_text):
    url = entry.get().strip()
    if not url or url == placeholder_text:
        messagebox.showwarning("Missing URL", "Please enter a valid URL.")
        return
    try:
        filename = asyncio.run(resolve_filename_from_url(url))
    except Exception as e:
        messagebox.showerror("Error", f"Failed to resolve URL: {str(e)}")
        logging.error(f"URL resolution failed: {str(e)}\n{traceback.format_exc()}")
        return

    save_path = filedialog.asksaveasfilename(initialfile=filename, title="Save As")
    if not save_path: return

    job = {"url": url, "output_file": save_path}
    app_state["download_queue"].append(job)
    listbox.insert(tk.END, f"{get_file_icon(save_path)} {os.path.basename(save_path)}")
    entry.delete(0, tk.END)
    # Manually trigger focus out to restore placeholder
    entry.focus_set()
    entry.master.focus_set()
    logging.info(f"Added to queue: {url} -> {save_path}")

def remove_from_queue(listbox):
    sel = listbox.curselection()
    if sel:
        # Iterate backwards to safely delete multiple selected items
        for idx in reversed(sel):
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
        app_state["stop_current_download"] = False
        update_button_states(gui)
        download_mode = gui['download_mode'].get()
        if download_mode == "Download Selected":
            sel = gui['queue_listbox'].curselection()
            if not sel:
                messagebox.showwarning("No Selection", "Please select an item to download.")
                app_state["is_processing"] = False
                update_button_states(gui)
                return
            selected_index = sel[0]
            run_async_task(process_queue_async(gui, selected_index=selected_index))
        else:
            run_async_task(process_queue_async(gui))

def pause_resume(gui):
    app_state["is_paused"] = not app_state["is_paused"]
    logging.info(f"Download {'paused' if app_state['is_paused'] else 'resumed'}.")
    update_button_states(gui)

def stop_download(gui):
    if app_state["is_processing"] and messagebox.askyesno("Stop Download", "Are you sure you want to stop the current download?"):
        app_state["stop_current_download"] = True
        app_state["is_processing"] = False
        update_button_states(gui)
        gui['root'].after(0, gui['current_file_label'].config, {"text": "Stopping..."})

def update_button_states(gui):
    processing = app_state["is_processing"]
    gui['start_button'].config(state='disabled' if processing else 'normal')
    gui['stop_button'].config(state='normal' if processing else 'disabled')
    gui['pause_resume_button'].config(state='normal' if processing else 'disabled', text="Resume" if app_state["is_paused"] else "Pause")
    gui['add_button'].config(state='disabled' if processing else 'normal')
    gui['remove_button'].config(state='disabled' if processing else 'normal')

def quit_app(icon, root):
    icon.stop()
    root.quit()
    root.destroy()
    os._exit(0)

def show_window(icon, root):
    icon.stop()
    root.after(0, root.deiconify)

def minimize_to_tray(root):
    root.withdraw()
    tray_icon_path = "icon.ico" # Make sure you have an icon.ico or icon.png
    try:
        image = Image.open(tray_icon_path)
    except FileNotFoundError:
        image = Image.new('RGB', (64, 64), color='skyblue')
    menu = pystray.Menu(pystray.MenuItem("Show", lambda: show_window(icon, root), default=True), pystray.MenuItem("Quit", lambda: quit_app(icon, root)))
    icon = pystray.Icon("PYDownloader", image, "PYDownloader", menu)
    threading.Thread(target=icon.run, daemon=True).start()

def main():
    root = ttk.Window(themename="darkly")
    root.title("PYDownloader")
    root.geometry("600x550")

    # --- URL Entry with Placeholder ---
    placeholder_text = "Paste your link here..."
    url_frame = ttk.Frame(root)
    url_frame.pack(fill="x", padx=10, pady=(10,5))
    url_entry = ttk.Entry(url_frame, font=("Segoe UI", 10))
    url_entry.pack(side="left", fill="x", expand=True, ipady=4)
    
    def on_focus_in(event):
        if url_entry.get() == placeholder_text:
            url_entry.delete(0, "end")
            url_entry.config(foreground=ttk.Style().lookup('TEntry', 'foreground'))
    def on_focus_out(event):
        if not url_entry.get():
            url_entry.insert(0, placeholder_text)
            url_entry.config(foreground='grey')

    url_entry.insert(0, placeholder_text)
    url_entry.config(foreground='grey')
    url_entry.bind("<FocusIn>", on_focus_in)
    url_entry.bind("<FocusOut>", on_focus_out)

    add_button_main = ttk.Button(url_frame, text="Add Link")
    add_button_main.pack(side="left", padx=(5,0), ipady=2)
    
    # --- Vertical Layout for Queue and Completed ---
    queue_frame = ttk.LabelFrame(root, text="Download Queue", padding=5)
    queue_frame.pack(fill="x", padx=5, pady=5)  # Removed expand=True
    queue_listbox = tk.Listbox(queue_frame, font=("Segoe UI", 10), selectmode="extended", height=5)
    queue_listbox.pack(fill="both", expand=True)
    #----controls---
    control_frame = ttk.Frame(root)
    control_frame.pack(fill="x", padx=10, pady=10)
    start_btn = ttk.Button(control_frame, text="Start", style='success.TButton')
    pause_btn = ttk.Button(control_frame, text="Pause", style='warning.TButton', state="disabled")
    stop_btn = ttk.Button(control_frame, text="Stop", style='danger.TButton', state="disabled")
    start_btn.pack(side="left", expand=True, fill="x", padx=2, ipady=2)
    pause_btn.pack(side="left", expand=True, fill="x", padx=2, ipady=2)
    stop_btn.pack(side="left", expand=True, fill="x", padx=2, ipady=2)    

    completed_frame = ttk.LabelFrame(root, text="Completed Downloads", padding=5)
    completed_frame.pack(fill="x", padx=10, pady=5)  # Removed expand=True
    completed_listbox = tk.Listbox(completed_frame, font=("Segoe UI", 10), height=3)
    completed_listbox.pack(fill="both", expand=True)


    url_entry.bind("<Return>", lambda event: add_to_queue(url_entry, queue_listbox, placeholder_text))

    # --- Progress and Status ---
    progress_frame = ttk.Frame(root)
    progress_frame.pack(fill="x", padx=10, pady=5)
    status = ttk.Label(progress_frame, text="Idle", font=("Segoe UI", 9))
    status.pack(anchor="w")
    progress_var = tk.DoubleVar()
    progress_bar = ttk.Progressbar(progress_frame, variable=progress_var, style="success.Striped.Horizontal.TProgressbar", length=400)
    progress_bar.pack(fill="x", pady=2)

    info_frame = ttk.Frame(root)
    info_frame.pack(fill="x", padx=10, pady=5)
    size_label = ttk.Label(info_frame, text="Size: N/A", font=("Segoe UI", 9))
    size_label.pack(side="left", padx=5)
    percent_label = ttk.Label(info_frame, text="Progress: 0.00%", font=("Segoe UI", 9))
    percent_label.pack(side="left", padx=5)
    speed_label = ttk.Label(info_frame, text="Speed: N/A", font=("Segoe UI", 9))
    speed_label.pack(side="left", padx=5)
    time_label = ttk.Label(info_frame, text="Time: N/A", font=("Segoe UI", 9))
    time_label.pack(side="left", padx=5)

    # --- Controls ---
    bottom_frame = ttk.Frame(root)
    bottom_frame.pack(fill="x", padx=10, pady=(5,10))
    remove_button = ttk.Button(bottom_frame, text="Remove Selected", command=lambda: remove_from_queue(queue_listbox))
    remove_button.pack(side="left", padx=2)
    open_completed_button = ttk.Button(bottom_frame, text="Open Folder", command=lambda: open_completed_folder(completed_listbox))
    open_completed_button.pack(side="left", padx=2)
    download_mode_var = tk.StringVar(value="Download All")
    download_mode = ttk.Combobox(bottom_frame, textvariable=download_mode_var, values=["Download All", "Download Selected"], state="readonly", width=18)
    download_mode.pack(side="left", padx=2)
    tray_btn = ttk.Button(bottom_frame, text="Minimize to Tray", command=lambda: minimize_to_tray(root))
    tray_btn.pack(side="left", padx=2)

    gui_controls = {
        "root": root, "queue_listbox": queue_listbox, "completed_listbox": completed_listbox,
        "current_file_label": status, "progress_var": progress_var, "start_button": start_btn,
        "pause_resume_button": pause_btn, "stop_button": stop_btn, "add_button": add_button_main,
        "remove_button": remove_button, "size_label": size_label, "time_label": time_label,
        "speed_label": speed_label, "percent_label": percent_label, "download_mode": download_mode_var
    }

    add_button_main.config(command=lambda: add_to_queue(url_entry, queue_listbox, placeholder_text))
    start_btn.config(command=lambda: start_queue(gui_controls))
    pause_btn.config(command=lambda: pause_resume(gui_controls))
    stop_btn.config(command=lambda: stop_download(gui_controls))

    threading.Thread(target=start_asyncio_loop, daemon=True).start()

    def on_close():
        if messagebox.askokcancel("Exit", "Are you sure you want to exit PYDownloader?"):
            root.destroy()
            os._exit(0)
    root.protocol("WM_DELETE_WINDOW", on_close)
    
    update_button_states(gui_controls)
    root.mainloop()

if __name__ == "__main__":
    main()