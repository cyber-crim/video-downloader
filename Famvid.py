import yt_dlp
import tkinter as tk
from tkinter import messagebox, filedialog
import json
try:
    import requests
except Exception:
    requests = None
import tkinter.ttk as ttk
from pathlib import Path
from datetime import datetime, timedelta
import threading
import time
import uuid
import math
import sys
import os

def download(content_type):
    url = url_entry.get().strip()

    if not url:
        # show a context-aware warning depending on requested content type
        if content_type == 'audio':
            messagebox.showwarning("Warning", "Please enter an audio URL!")
        else:
            messagebox.showwarning("Warning", "Please enter a video URL!")
        return

    # if nothing provided at all, show a warning
    if not url:
        if content_type == 'audio':
            messagebox.showwarning("Warning", "Please enter an audio URL or a search term!")
        else:
            messagebox.showwarning("Warning", "Please enter a video URL or a search term!")
        return

    # Ask where to save
    save_path = filedialog.askdirectory(title="Select Download Folder")
    if not save_path:
        return  # cancelled

    # indicate start and prevent duplicate clicks
    try:
        progress_label.config(text='Starting download...')
        video_btn.config(state='disabled')
        audio_btn.config(state='disabled')
    except Exception:
        pass

    # Determine subscription state and choose quality limits
    subscribed = is_subscribed()

    if content_type == "video":
        # Free users: limit to SD (<=480p). Subscribers: full quality.
        if subscribed:
            fmt = 'best'
        else:
            fmt = 'best[height<=480]'
        options = {
            'outtmpl': f'{save_path}/%(title)s.%(ext)s',
            'format': fmt,
        }
    else:  # audio
        # Free users: lower bitrate audio. Subscribers: full audio.
        if subscribed:
            fmt = 'bestaudio/best'
        else:
            # try to pick audio with bitrate <=128kbps if available
            fmt = 'bestaudio[abr<=128]/bestaudio/best'
        options = {
            'format': fmt,
            'outtmpl': f'{save_path}/%(title)s.%(ext)s',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        }
    # progress hook to update UI
    def progress_hook(d):
        # check for global control
        ctrl = download_controls.get('current')
        if ctrl:
            if ctrl['stop'].is_set():
                # raise a download error to abort
                raise yt_dlp.utils.DownloadError('Download cancelled by user')
            # if paused, block progress updates until unpaused
            while ctrl['pause'].is_set():
                time.sleep(0.2)

        status = d.get('status')
        if status == 'downloading':
            # raw metrics from yt-dlp
            downloaded = d.get('downloaded_bytes') or 0
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            inst_speed = d.get('speed') or 0
            # initialize stats container
            stats = ctrl.setdefault('_stats', {
                'last_time': time.time(),
                'last_downloaded': downloaded,
                'ema_speed': float(inst_speed or 0.0),
                'ema_percent': None,
                'last_ui': 0,
            })

            now = time.time()
            dt = max(0.0001, now - stats['last_time'])
            delta = max(0, downloaded - stats['last_downloaded'])
            inst_speed = delta / dt if dt > 0 else inst_speed
            # exponential moving average (smoothing)
            alpha = 0.25
            stats['ema_speed'] = (alpha * inst_speed) + (1 - alpha) * stats.get('ema_speed', inst_speed)
            percent = (downloaded / total * 100) if total else None
            if percent is not None:
                if stats['ema_percent'] is None:
                    stats['ema_percent'] = percent
                else:
                    stats['ema_percent'] = (alpha * percent) + (1 - alpha) * stats['ema_percent']

            stats['last_time'] = now
            stats['last_downloaded'] = downloaded

            # throttle UI updates to ~5x/sec to stabilize
            if now - stats['last_ui'] < 0.18:
                return
            stats['last_ui'] = now

            # prepare UI values
            human_dl = f"{downloaded/1024/1024:.2f} MB"
            human_total = f"{total/1024/1024:.2f} MB" if total else 'Unknown'
            speed_mb = stats['ema_speed'] / 1024 / 1024
            # ETA calculation
            eta = None
            if total and stats['ema_speed'] > 0:
                remaining = max(0, total - downloaded)
                eta = int(remaining / stats['ema_speed'])

            # schedule UI update on main thread
            def ui_update():
                try:
                    if total:
                        # determinate mode
                        progress_bar.config(mode='determinate')
                        pct = stats['ema_percent'] if stats['ema_percent'] is not None else (percent or 0)
                        pct = max(0.0, min(100.0, pct))
                        progress_var.set(pct)
                    else:
                        # indeterminate when we don't know total
                        progress_bar.config(mode='indeterminate')
                        try:
                            progress_bar.start(10)
                        except Exception:
                            pass

                    speed_str = f"{speed_mb:.2f} MB/s" if speed_mb >= 0 else '0.00 MB/s'
                    eta_str = f"ETA: {eta}s" if eta is not None else ''
                    pct_display = f"{(stats['ema_percent'] if stats['ema_percent'] is not None else (percent or 0)):.1f}%" if total else ''
                    progress_label.config(text=f"Downloading: {d.get('filename')} — {human_dl} / {human_total} — {pct_display} — {speed_str} {eta_str}")
                except Exception:
                    pass

            root.after(0, ui_update)
        elif status == 'finished':
            def ui_done():
                progress_var.set(100)
                progress_label.config(text=f"Finished: {d.get('filename')}")
            root.after(0, ui_done)

    options['progress_hooks'] = [progress_hook]
    # helper that starts the download using the prepared options
    def _start_with_options(opts):
        try:
            opts['progress_hooks'] = [progress_hook]
            # prepare control dict and start threaded download
            ctrl = {
                'stop': threading.Event(),
                'pause': threading.Event(),
                'thread': None,
                'url': url,
                'save_path': save_path,
                'type': content_type,
                'options': opts,
            }
            download_controls['current'] = ctrl
            _set_controls_enabled(True)
            t = threading.Thread(target=lambda: start_download_thread(opts, ctrl), daemon=True)
            ctrl['thread'] = t
            t.start()
        except Exception as e:
            messagebox.showerror("Error", f"❌ Something went wrong:\n{e}")

    # Present a modal quality selection dialog to the user
    def show_quality_dialog():
        # Build choices based on type and subscription
        choices = []  # list of (label, format_string)
        if content_type == 'video':
            # show common video heights; subscribed users see high options
            video_choices = [
                ("Best (auto)", 'best'),
                ("1080p", 'best[height<=1080]'),
                ("720p", 'best[height<=720]'),
                ("480p", 'best[height<=480]'),
                ("360p", 'best[height<=360]'),
                ("240p", 'best[height<=240]'),
            ]
            if subscribed:
                choices = video_choices
            else:
                # free users limited to <=480
                choices = [c for c in video_choices if '<=480' in c[1] or c[1] == 'best']
        else:
            # audio choices - different bitrate options
            audio_choices = [
                ("Best audio", 'bestaudio/best'),
                ("MP3 ~192kbps", 'bestaudio/best'),
                ("MP3 <=128kbps", 'bestaudio[abr<=128]/bestaudio/best'),
                ("MP3 <=64kbps", 'bestaudio[abr<=64]/bestaudio/best'),
            ]
            if subscribed:
                choices = audio_choices
            else:
                # free users prefer lower bitrate option first
                choices = [audio_choices[2], audio_choices[0], audio_choices[3]]

        dlg = tk.Toplevel(root)
        dlg.title('Choose quality')
        dlg.geometry('360x300')
        tk.Label(dlg, text='Select desired quality:', font=(None, 12, 'bold')).pack(pady=8)
        var = tk.StringVar(value=options.get('format', choices[0][1] if choices else ''))
        frame = tk.Frame(dlg)
        frame.pack(fill='both', expand=True, padx=12)
        for label, fmt_str in choices:
            tk.Radiobutton(frame, text=label, variable=var, value=fmt_str, anchor='w', justify='left').pack(fill='x', pady=2)

        selected = {'ok': False}

        def on_confirm():
            selected['ok'] = True
            dlg.destroy()

        def on_cancel():
            selected['ok'] = False
            dlg.destroy()

        btns = tk.Frame(dlg)
        btns.pack(pady=8)
        tk.Button(btns, text='Download', command=on_confirm, bg='#4CAF50', fg='white').pack(side='left', padx=8)
        tk.Button(btns, text='Cancel', command=on_cancel).pack(side='left', padx=8)

        dlg.transient(root)
        dlg.grab_set()
        root.wait_window(dlg)

        if not selected['ok']:
            return None
        return var.get()

    # show the dialog and act on the selection
    chosen_fmt = show_quality_dialog()
    if not chosen_fmt:
        # user cancelled quality selection
        # re-enable buttons
        try:
            video_btn.config(state='normal')
            audio_btn.config(state='normal')
        except Exception:
            pass
        return

    # apply the user's choice and start
    options['format'] = chosen_fmt
    _start_with_options(options)


def start_download_thread(options, ctrl):
    url = ctrl['url']
    try:
        # run yt-dlp with the provided options; the progress_hook will update UI
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([url])
        # log activity
        log_activity({'type': ctrl['type'], 'url': url, 'time': datetime.utcnow().isoformat()})
        root.after(0, lambda: messagebox.showinfo("Success", f"✅ {ctrl['type'].capitalize()} downloaded successfully!"))
    except Exception as e:
        root.after(0, lambda: messagebox.showerror("Error", f"❌ Something went wrong:\n{e}"))
        log_activity({'type': 'error', 'message': str(e), 'time': datetime.utcnow().isoformat()})
    finally:
        def _finish():
            progress_var.set(0)
            progress_label.config(text='')
            download_controls['current'] = None
            _set_controls_enabled(False)
            try:
                video_btn.config(state='normal')
                audio_btn.config(state='normal')
            except Exception:
                pass
        root.after(0, _finish)

# --- GUI setup ---
# Initialize root
root = tk.Tk()
root.title("Famvid")
# Helper for locating bundled data files (works with PyInstaller --onefile)
def resource_path(relative_path: str) -> str:
    """Return an absolute path to a resource, whether running from source or a PyInstaller bundle."""
    base_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)

# Try to set a player icon (player.ico or player.png) located next to the script or bundled data; ignore failures
try:
    ico_path = Path(resource_path('player.ico'))
    png_path = Path(resource_path('player.png'))
    if ico_path.exists():
        root.iconbitmap(str(ico_path))
    elif png_path.exists():
        try:
            img = tk.PhotoImage(file=str(png_path))
            root.iconphoto(False, img)
        except Exception:
            pass
except Exception:
    pass

# Start maximized on Windows; fall back to a sensible default
try:
    root.state('zoomed')
except Exception:
    root.geometry("900x600")
root.minsize(700, 450)

# Top navigation
top_nav = tk.Frame(root, bd=1, relief='raised')
top_nav.pack(side='top', fill='x')

# Header label (app name) with a bold, commonly-available Windows font
try:
    import tkinter.font as tkfont
    header_font = ("Segoe UI", 16, "bold")
except Exception:
    header_font = (None, 16, "bold")

tk.Label(top_nav, text='Famvid', font=header_font).pack(side='left', padx=10)

# Navigation buttons with simple emoji icons
btn_home = tk.Button(top_nav, text='🏠 Home')
btn_back = tk.Button(top_nav, text='🔙 Back', state='disabled')
btn_home.pack(side='left', padx=6, pady=6)
btn_back.pack(side='left', padx=6, pady=6)

# Main container for pages
container = tk.Frame(root)
container.pack(fill='both', expand=True)

# Home page widgets (will be placed in a Frame)
home_frame = tk.Frame(container)
tk.Label(home_frame, text="Add your URL:", font=("Arial", 14)).pack(pady=12)
url_entry = tk.Entry(home_frame, width=80)
url_entry.pack(pady=6)


# --- Subscription UI and helpers ---
SUB_FILE = Path.home() / '.famvid_subscription.json'
CONFIG_FILE = Path.home() / '.famvid_config.json'
ACTIVITIES_FILE = Path.home() / '.famvid' / 'activities.json'
TRENDING_CACHE = Path.home() / '.famvid' / 'trending.json'


def load_subscription():
    try:
        if SUB_FILE.exists():
            data = json.loads(SUB_FILE.read_text(encoding='utf-8'))
            expiry = data.get('expiry')
            if expiry:
                return datetime.fromisoformat(expiry)
    except Exception:
        pass
    return None


def save_subscription(expiry_dt: datetime):
    try:
        SUB_FILE.write_text(json.dumps({'expiry': expiry_dt.isoformat()}), encoding='utf-8')
    except Exception:
        pass


def is_subscribed() -> bool:
    expiry = load_subscription()
    if expiry and expiry > datetime.utcnow():
        return True
    return False


def update_subscription_label():
    expiry = load_subscription()
    if expiry and expiry > datetime.utcnow():
        subscription_label.config(text=f"Plan: Subscribed (expires {expiry.date()})", fg='green')
    else:
        subscription_label.config(text="Plan: Free", fg='orange')


def load_config():
    try:
        if CONFIG_FILE.exists():
            return json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
    except Exception:
        pass
    return {}


def save_config(cfg: dict):
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg), encoding='utf-8')
    except Exception:
        pass


def log_activity(entry: dict):
    try:
        ACTIVITIES_FILE.parent.mkdir(parents=True, exist_ok=True)
        activities = []
        if ACTIVITIES_FILE.exists():
            activities = json.loads(ACTIVITIES_FILE.read_text(encoding='utf-8'))
        activities.insert(0, entry)
        # keep recent 200
        activities = activities[:200]
        ACTIVITIES_FILE.write_text(json.dumps(activities), encoding='utf-8')
    except Exception:
        pass


def load_activities():
    try:
        if ACTIVITIES_FILE.exists():
            return json.loads(ACTIVITIES_FILE.read_text(encoding='utf-8'))
    except Exception:
        pass
    return []


def open_subscription_dialog():
    # Simple dialog with payment instructions and a manual activation button.
    dlg = tk.Toplevel(root)
    dlg.title('Subscribe')
    dlg.geometry('420x220')
    tk.Label(dlg, text='Subscribe for 30 days (TSH 5,000)', font=(None, 12, 'bold')).pack(pady=8)
    tk.Label(dlg, text='Send TSH 5,000 to Vodacom number: 0694859071\nAccount/Name: Nassoro Nassoro', justify='left').pack(pady=6)
    tk.Label(dlg, text='After payment, press Confirm to activate your 30-day subscription.\n(This app does not verify payments automatically.)', wraplength=380, justify='left').pack(pady=8)

    def confirm_payment():
        expiry = datetime.utcnow() + timedelta(days=30)
        save_subscription(expiry)
        update_subscription_label()
        messagebox.showinfo('Subscribed', f'Your subscription is active until {expiry.date()}')
        dlg.destroy()

    def request_activation():
        # Create a pending request entry so admin can match payment and auto-approve
        reqs_dir = Path.home() / '.famvid' / 'requests'
        reqs_dir.mkdir(parents=True, exist_ok=True)
        req = {
            'id': str(uuid.uuid4()),
            'device': str(uuid.getnode()),
            'time': datetime.utcnow().isoformat(),
        }
        req_file = reqs_dir / (req['id'] + '.json')
        req_file.write_text(json.dumps(req), encoding='utf-8')
        messagebox.showinfo('Requested', 'Activation request created. Admin will approve after payment is verified.')


    btn_frame = tk.Frame(dlg)
    btn_frame.pack(pady=10)
    tk.Button(btn_frame, text='Confirm Payment (Activate)', command=confirm_payment, bg='#4CAF50', fg='white').grid(row=0, column=0, padx=8)
    tk.Button(btn_frame, text='Request Activation', command=request_activation).grid(row=0, column=1, padx=8)
    tk.Button(btn_frame, text='Close', command=dlg.destroy).grid(row=0, column=2, padx=8)


# Pages: Subscribe page
subscribe_frame = tk.Frame(container)
tk.Label(subscribe_frame, text='Subscribe', font=(None, 16, 'bold')).pack(pady=10)

sub_frame_inner = tk.Frame(subscribe_frame)
sub_frame_inner.pack(pady=6)
subscription_label = tk.Label(sub_frame_inner, text='Plan: Free', font=(None, 11))
subscription_label.pack(side='left', padx=(0, 8))
tk.Button(sub_frame_inner, text='Subscribe', command=open_subscription_dialog).pack(side='left')

update_subscription_label()


# Home page buttons
home_btn_frame = tk.Frame(home_frame)
home_btn_frame.pack(pady=12)
video_btn = tk.Button(home_btn_frame, text="Download Video", command=lambda: download("video"), bg="#4CAF50", fg="white", width=20)
video_btn.grid(row=0, column=0, padx=8)
audio_btn = tk.Button(home_btn_frame, text="Download Audio (MP3)", command=lambda: download("audio"), bg="#2196F3", fg="white", width=22)
audio_btn.grid(row=0, column=1, padx=8)

# Progress area
progress_frame = tk.Frame(home_frame)
progress_frame.pack(pady=8, fill='x')
progress_label = tk.Label(progress_frame, text='')
progress_label.pack(anchor='w', padx=10)
progress_var = tk.DoubleVar(value=0.0)
progress_bar = ttk.Progressbar(progress_frame, variable=progress_var, maximum=100)
progress_bar.pack(fill='x', padx=10, pady=4)

# Download controls
download_controls = {'current': None}

def _set_controls_enabled(enabled: bool):
    try:
        state = 'normal' if enabled else 'disabled'
        cancel_btn.config(state=state)
        pause_btn.config(state=state)
        restart_btn.config(state=state)
    except Exception:
        pass

def cancel_download():
    ctrl = download_controls.get('current')
    if not ctrl:
        return
    ctrl['stop'].set()
def toggle_pause():
    """Toggle pause/resume for the current download.

    This is a soft pause: it blocks progress-hook updates until resumed.
    """
    ctrl = download_controls.get('current')
    if not ctrl:
        return
    if ctrl['pause'].is_set():
        ctrl['pause'].clear()
        pause_btn.config(text='Pause')
    else:
        ctrl['pause'].set()
        pause_btn.config(text='Resume')


def restart_download():
    """Stop the current download (if any) and start a fresh run with the same options."""
    ctrl = download_controls.get('current')
    if not ctrl:
        return
    # request the running download to stop
    ctrl['stop'].set()

    def _wait_and_restart():
        # wait briefly for previous thread to end
        t = ctrl.get('thread')
        if t:
            t.join(timeout=10)

        # reuse stored options if available
        opts = ctrl.get('options')
        if opts is None:
            return

        new_ctrl = {
            'stop': threading.Event(),
            'pause': threading.Event(),
            'thread': None,
            'url': ctrl.get('url'),
            'save_path': ctrl.get('save_path'),
            'type': ctrl.get('type'),
            'options': opts,
        }
        download_controls['current'] = new_ctrl
        _set_controls_enabled(True)
        t2 = threading.Thread(target=lambda: start_download_thread(opts, new_ctrl), daemon=True)
        new_ctrl['thread'] = t2
        t2.start()

    threading.Thread(target=_wait_and_restart, daemon=True).start()

cancel_btn = tk.Button(progress_frame, text='Cancel', command=cancel_download, state='disabled')
cancel_btn.pack(side='right', padx=6)
pause_btn = tk.Button(progress_frame, text='Pause', command=toggle_pause, state='disabled')
pause_btn.pack(side='right', padx=6)
restart_btn = tk.Button(progress_frame, text='Restart', command=restart_download, state='disabled')
restart_btn.pack(side='right', padx=6)

# Recent Activities page
activities_frame = tk.Frame(container)
tk.Label(activities_frame, text='Recent Activities', font=(None, 16, 'bold')).pack(pady=8)
try:
    try:
        from PIL import Image, ImageTk
    except Exception:
        Image = None
        ImageTk = None
except Exception:
    Image = None
    ImageTk = None

activities_tree = ttk.Treeview(activities_frame, columns=('time', 'type', 'url'), show='headings')
activities_tree.heading('time', text='Time')
activities_tree.heading('type', text='Type')
activities_tree.heading('url', text='URL')
activities_tree.column('time', width=180)
activities_tree.column('type', width=80)
activities_tree.column('url', width=700)
activities_tree.pack(padx=10, pady=6, fill='both', expand=True)

# Trending page
trending_frame = tk.Frame(container)
tk.Label(trending_frame, text='Trending', font=(None, 16, 'bold')).pack(pady=8)
trending_tree = ttk.Treeview(trending_frame, columns=('title', 'channel', 'views', 'url'), show='headings', height=15)
trending_tree.heading('title', text='Title')
trending_tree.heading('channel', text='Channel')
trending_tree.heading('views', text='Views')
trending_tree.heading('url', text='URL')
trending_tree.column('title', width=420)
trending_tree.column('channel', width=200)
trending_tree.column('views', width=100)
trending_tree.column('url', width=260)
trending_tree.pack(padx=10, pady=6, fill='both', expand=True)

# thumbnail cache in-memory to avoid GC
thumbnail_cache = {}


def fetch_and_attach_thumbnail(item_id, thumb_url):
    try:
        thumbs_dir = TRENDING_CACHE.parent / 'thumbs'
        thumbs_dir.mkdir(parents=True, exist_ok=True)
        # determine local path
        fn = thumb_url.split('/')[-1].split('?')[0]
        local = thumbs_dir / fn
        if not local.exists():
            r = requests.get(thumb_url, timeout=10)
            r.raise_for_status()
            local.write_bytes(r.content)
        # load, resize and attach
        img = Image.open(local)
        img.thumbnail((120, 67))
        tkimg = ImageTk.PhotoImage(img)
        thumbnail_cache[item_id] = tkimg
        trending_tree.item(item_id, image=tkimg)
    except Exception:
        pass

def open_settings():
    dlg = tk.Toplevel(root)
    dlg.title('Settings')
    dlg.geometry('420x160')
    cfg = load_config()
    tk.Label(dlg, text='YouTube API Key (optional):').pack(pady=6)
    key_entry = tk.Entry(dlg, width=60)
    key_entry.pack(pady=6)
    key_entry.insert(0, cfg.get('youtube_api_key', ''))

    def save():
        cfg['youtube_api_key'] = key_entry.get().strip()
        save_config(cfg)
        dlg.destroy()

    tk.Button(dlg, text='Save', command=save, bg='#4CAF50', fg='white').pack(pady=8)

tk.Button(top_nav, text='⚙ Settings', command=open_settings).pack(side='right', padx=6)


def refresh_activities():
    for i in activities_tree.get_children():
        activities_tree.delete(i)
    for a in load_activities():
        activities_tree.insert('', 'end', values=(a.get('time'), a.get('type'), a.get('url')))


def fetch_trending():
    cfg = load_config()
    api_key = cfg.get('youtube_api_key')
    if not api_key:
        # show message in trending_tree
        for i in trending_tree.get_children():
            trending_tree.delete(i)
        trending_tree.insert('', 'end', values=('No API key configured. Set YouTube API key in Settings to fetch trending.', '', '', ''))
        return
    if requests is None:
        for i in trending_tree.get_children():
            trending_tree.delete(i)
        trending_tree.insert('', 'end', values=('requests library not installed. Run: pip install requests', '', '', ''))
        return
    # call YouTube Data API (videos.list chart=mostPopular)
    url = 'https://www.googleapis.com/youtube/v3/videos'
    params = {'part': 'snippet,statistics', 'chart': 'mostPopular', 'maxResults': 20, 'regionCode': 'US', 'key': api_key}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        items = data.get('items', [])
        for i in trending_tree.get_children():
            trending_tree.delete(i)
        for it in items:
            title = it['snippet']['title']
            chan = it['snippet']['channelTitle']
            views = it.get('statistics', {}).get('viewCount', '0')
            vid = it['id']
            thumb = it['snippet']['thumbnails'].get('high', {}).get('url') or it['snippet']['thumbnails'].get('default', {}).get('url')
            item_id = trending_tree.insert('', 'end', values=(title, chan, views, f'https://youtu.be/{vid}'))
            if thumb:
                threading.Thread(target=fetch_and_attach_thumbnail, args=(item_id, thumb), daemon=True).start()
        # cache results
        TRENDING_CACHE.parent.mkdir(parents=True, exist_ok=True)
        TRENDING_CACHE.write_text(json.dumps(items), encoding='utf-8')
    except Exception:
        # fallback to cache
        try:
            items = json.loads(TRENDING_CACHE.read_text(encoding='utf-8'))
            for i in trending_tree.get_children():
                trending_tree.delete(i)
            for it in items:
                title = it['snippet']['title']
                chan = it['snippet']['channelTitle']
                vid = it['id']
                thumb = it['snippet'].get('thumbnails', {}).get('default', {}).get('url')
                item_id = trending_tree.insert('', 'end', values=(title, chan, '', f'https://youtu.be/{vid}'))
                if thumb:
                    threading.Thread(target=fetch_and_attach_thumbnail, args=(item_id, thumb), daemon=True).start()
        except Exception:
            for i in trending_tree.get_children():
                trending_tree.delete(i)
            trending_tree.insert('', 'end', values=('Failed to fetch trending and no cache available.', '', '', ''))


def refresh_trending_background():
    t = threading.Thread(target=fetch_trending, daemon=True)
    t.start()


tk.Button(top_nav, text='Activities', command=lambda: (show_frame(activities_frame), refresh_activities())).pack(side='right', padx=6)
tk.Button(top_nav, text='Trending', command=lambda: (show_frame(trending_frame), refresh_trending_background())).pack(side='right', padx=6)

# --- Auto-approve watcher (local payments) ---
PAYMENTS_FILE = Path.home() / '.famvid' / 'payments.json'
REQUESTS_DIR = Path.home() / '.famvid' / 'requests'


def load_payments():
    try:
        if PAYMENTS_FILE.exists():
            return json.loads(PAYMENTS_FILE.read_text(encoding='utf-8'))
    except Exception:
        pass
    return []


def save_payments(payments):
    try:
        PAYMENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        PAYMENTS_FILE.write_text(json.dumps(payments), encoding='utf-8')
    except Exception:
        pass


def watcher_loop(stop_event: threading.Event):
    seen = set()
    while not stop_event.is_set():
        # load payments and pending requests
        payments = load_payments()
        for p in payments:
            pid = p.get('txid') or p.get('id')
            if not pid or pid in seen:
                continue
            # try to match pending requests by device or custom ref
            for rf in REQUESTS_DIR.glob('*.json'):
                try:
                    req = json.loads(rf.read_text(encoding='utf-8'))
                except Exception:
                    continue
                # simple match: device == payer (if payer provided as device)
                if str(req.get('device')) == str(p.get('device')) or p.get('ref') == req.get('id'):
                    # approve
                    expiry = datetime.utcnow() + timedelta(days=30)
                    save_subscription(expiry)
                    update_subscription_label()
                    # move request to approved
                    approved_dir = REQUESTS_DIR.parent / 'approved'
                    approved_dir.mkdir(parents=True, exist_ok=True)
                    rf.replace(approved_dir / rf.name)
                    seen.add(pid)
                    break
        time.sleep(5)


stop_watcher = threading.Event()
watcher_thread = threading.Thread(target=watcher_loop, args=(stop_watcher,), daemon=True)
watcher_thread.start()

# Buttons for each option
button_frame = tk.Frame(root)
button_frame.pack(pady=15)



history_stack = []


def show_frame(frame, push=True):
    # push current to history if requested
    current = container.winfo_children()
    if push and current:
        history_stack.append(current[0])
    for child in container.winfo_children():
        child.pack_forget()
    frame.pack(fill='both', expand=True)
    # enable/disable back button
    btn_back.config(state='normal' if history_stack else 'disabled')


def go_home():
    show_frame(home_frame)


def go_subscribe():
    show_frame(subscribe_frame)


def go_back():
    if not history_stack:
        return
    prev = history_stack.pop()
    for child in container.winfo_children():
        child.pack_forget()
    prev.pack(fill='both', expand=True)
    btn_back.config(state='normal' if history_stack else 'disabled')


btn_home.config(command=go_home)
btn_back.config(command=go_back)

# Initially show home
show_frame(home_frame, push=False)

tk.Label(root, text="Powered by sparke electronics", font=("sergio", 8), fg="gray").pack(side="bottom", pady=5)

tk.Button(top_nav, text='💳 Subscribe', command=go_subscribe).pack(side='right', padx=6)

root.mainloop()