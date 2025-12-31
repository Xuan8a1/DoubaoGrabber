import os
import sys
import ctypes
import json
import time
import threading
import requests
import io
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, HTTPServer

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from PIL import Image

# PyQt6 相关
from PyQt6.QtCore import Qt, QUrl, pyqtSignal, QThread, QSize, QObject, QEvent
from PyQt6.QtGui import QDesktopServices, QPixmap, QIcon, QFont, QColor, QImage
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, 
    QGraphicsOpacityEffect, QLabel, QFrame, QFileDialog
)

# Fluent Widgets 相关
from qfluentwidgets import (
    FluentWindow, FluentIcon as FIF, TitleLabel, SubtitleLabel, 
    PrimaryPushButton, ScrollArea, SettingCardGroup, SettingCard, 
    InfoBar, IndeterminateProgressRing, ComboBox, 
    setTheme, Theme, setThemeColor, qconfig, 
    BodyLabel, CaptionLabel, StrongBodyLabel,
    TransparentToolButton, AvatarWidget, SimpleCardWidget,
    CheckBox, TextEdit, PushButton, CardWidget
)

# ==========================================================
# 0. 全局配置 & 工具
# ==========================================================
def init_taskbar_icon():
    if sys.platform == "win32":
        my_app_id = 'doubao.downloader.v2' 
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(my_app_id)
        except: pass

init_taskbar_icon()

def resource_path(relative_path):
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)

os.environ["QT_API"] = "pyqt6"
CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".gh_auth_config.json")
SAVE_DIR = "doubao_selected_images"
if not os.path.exists(SAVE_DIR):
    os.makedirs(SAVE_DIR)

# ==========================================================
# 1. 豆包核心逻辑 (Selenium & Hook)
# ==========================================================
JS_HOOK_CODE = """
if (!window._db_hook_active) {
    window._db_captured_urls = [];
    
    function findAllKeysInJson(obj, key) {
        const results = [];
        function search(current) {
            if (current && typeof current === "object") {
                if (!Array.isArray(current) && Object.prototype.hasOwnProperty.call(current, key)) {
                    results.push(current[key]);
                }
                const items = Array.isArray(current) ? current : Object.values(current);
                for (const item of items) {
                    search(item);
                }
            }
        }
        search(obj);
        return results;
    }

    const _orig_parse = JSON.parse;
    JSON.parse = function(text) {
        const data = _orig_parse(text);
        try {
            if (text.includes("creations")) {
                let creations = findAllKeysInJson(data, "creations");
                if (creations.length > 0) {
                    creations.forEach(creation => {
                        if (Array.isArray(creation)) {
                            creation.map(item => {
                                const rawUrl = item?.image?.image_ori_raw?.url;
                                if (rawUrl) {
                                    if (!window._db_captured_urls.includes(rawUrl)) {
                                        window._db_captured_urls.push(rawUrl);
                                    }
                                }
                            });
                        }
                    });
                }
            }
        } catch (e) { console.error(e); }
        return data;
    };
    window._db_hook_active = true;
}
return window._db_captured_urls;
"""

class BrowserWorker(QThread):
    """负责 Selenium 浏览器的启动和循环监听"""
    log_signal = pyqtSignal(str)
    new_image_signal = pyqtSignal(str)
    browser_closed_signal = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.driver = None
        self.is_monitoring = False
        self.captured_urls = set()
        self.running = True

    def launch_browser(self):
        try:
            self.log_signal.emit("正在启动 Chrome 浏览器...")
            opts = webdriver.ChromeOptions()
            opts.add_argument("--disable-blink-features=AutomationControlled")
            opts.add_experimental_option("excludeSwitches", ["enable-automation"])
            self.driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
            self.driver.get("https://www.doubao.com/chat")
            self.log_signal.emit("浏览器已启动，请登录豆包账号。")
        except Exception as e:
            self.log_signal.emit(f"启动失败: {str(e)}")

    def set_monitoring(self, enable):
        self.is_monitoring = enable
        status = "开启" if enable else "暂停"
        self.log_signal.emit(f"监听已{status}")

    def run(self):
        while self.running:
            if self.is_monitoring and self.driver:
                try:
                    try: _ = self.driver.window_handles
                    except: 
                        self.log_signal.emit("浏览器已关闭")
                        self.driver = None
                        self.is_monitoring = False
                        self.browser_closed_signal.emit()
                        continue

                    urls = self.driver.execute_script(JS_HOOK_CODE)
                    if urls:
                        new_urls = [u for u in urls if u not in self.captured_urls]
                        for url in new_urls:
                            self.captured_urls.add(url)
                            self.new_image_signal.emit(url)
                    
                    time.sleep(1.5)
                except Exception as e:
                    pass
            else:
                time.sleep(1)

    def stop(self):
        self.running = False
        self.is_monitoring = False
        if self.driver:
            try: self.driver.quit()
            except: pass

class ThumbnailWorker(QThread):
    """异步下载缩略图，防止卡顿 UI"""
    loaded = pyqtSignal(str, QPixmap) # url, pixmap

    def __init__(self, url):
        super().__init__()
        self.url = url

    def run(self):
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            resp = requests.get(self.url, headers=headers, timeout=15)
            if resp.status_code == 200:
                img_data = resp.content
                image = Image.open(io.BytesIO(img_data))
                
                # 调整大小用于预览 (高度 150)
                base_height = 150
                h_percent = (base_height / float(image.size[1]))
                w_size = int((float(image.size[0]) * float(h_percent)))
                image = image.resize((w_size, base_height), Image.Resampling.LANCZOS)
                
                # 【修复颜色核心代码】
                # 直接转为RGBA，Pillow会自动处理通道顺序，不再需要手动split/merge
                image = image.convert("RGBA")
                data = image.tobytes("raw", "RGBA")
                
                qim = QImage(data, image.size[0], image.size[1], QImage.Format.Format_RGBA8888)
                # 必须copy一下，否则data被回收后图片会花屏或黑屏
                qim = qim.copy() 
                pix = QPixmap.fromImage(qim)
                
                self.loaded.emit(self.url, pix)
        except Exception:
            pass

# ==========================================================
# 2. 持久化管理 (保持原样)
# ==========================================================
class AuthConfig:
    @staticmethod
    def save(token, user_data):
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump({"token": token, "user": user_data}, f)
        except: pass

    @staticmethod
    def load():
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except: return None
        return None

    @staticmethod
    def clear():
        if os.path.exists(CONFIG_FILE):
            try: os.remove(CONFIG_FILE)
            except: pass

# ================= GitHub OAuth 配置 =================
CLIENT_ID = "Ov23li4ZIgQqInSyaguO"
CLIENT_SECRET = "52293848be54a807f1301c5b70e4c63f1c66c396"
REDIRECT_URI = "http://localhost:8000/callback"

# ==========================================================
# 3. 后台线程逻辑
# ==========================================================
class OAuthHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args): pass
    def do_GET(self):
        parsed_path = urlparse(self.path)
        if parsed_path.path == "/callback":
            query = parse_qs(parsed_path.query)
            if "code" in query:
                self.server.auth_code = query["code"][0]
                self.send_response(200)
                self.send_header("Content-type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write("<html><body style='text-align:center;padding-top:50px;font-family:sans-serif;'><h1>登录成功</h1><p>请返回应用程序。</p></body></html>".encode("utf-8"))
            else: self.send_error(400)
        else: self.send_error(404)

class LoginWorker(QThread):
    login_success = pyqtSignal(dict, str)
    login_failed = pyqtSignal(str)

    def run(self):
        server = None
        try:
            server = HTTPServer(('localhost', 8000), OAuthHandler)
            server.auth_code = None
            auth_url = f"https://github.com/login/oauth/authorize?client_id={CLIENT_ID}&redirect_uri={REDIRECT_URI}&scope=user"
            QDesktopServices.openUrl(QUrl(auth_url))
            server.handle_request()
            if getattr(server, 'auth_code', None):
                res = requests.post("https://github.com/login/oauth/access_token", 
                    json={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "code": server.auth_code},
                    headers={"Accept": "application/json"}
                )
                token = res.json().get("access_token")
                if token:
                    user_res = requests.get("https://api.github.com/user", headers={"Authorization": f"token {token}"})
                    if user_res.status_code == 200: self.login_success.emit(user_res.json(), token)
                    else: self.login_failed.emit("拉取用户信息失败")
                else: self.login_failed.emit("Token 获取失败")
            else: self.login_failed.emit("授权取消")
        except Exception as e: self.login_failed.emit(str(e))
        finally: 
            if server: server.server_close()

class VerifyWorker(QThread):
    verify_finished = pyqtSignal(bool, dict, str)
    def __init__(self, token):
        super().__init__()
        self.token = token
    def run(self):
        try:
            res = requests.get("https://api.github.com/user", headers={"Authorization": f"token {self.token}"}, timeout=5)
            if res.status_code == 200: self.verify_finished.emit(True, res.json(), self.token)
            else: self.verify_finished.emit(False, {}, "")
        except: self.verify_finished.emit(False, {}, "")

class AvatarWorker(QThread):
    avatar_loaded = pyqtSignal(QPixmap)
    def __init__(self, url):
        super().__init__()
        self.url = url
    def run(self):
        try:
            res = requests.get(self.url, timeout=10)
            pix = QPixmap()
            pix.loadFromData(res.content)
            self.avatar_loaded.emit(pix)
        except: pass

# ==========================================================
# 4. UI 界面类
# ==========================================================

class ImageCard(CardWidget):
    """自定义卡片，显示单张图片和选择框"""
    def __init__(self, url, pixmap, parent=None):
        super().__init__(parent)
        self.url = url
        self.setFixedSize(360, 180) # 固定大小卡片
        
        self.hLayout = QHBoxLayout(self)
        self.hLayout.setContentsMargins(10, 10, 10, 10)
        
        # 左侧：选框
        self.check = CheckBox(self)
        self.check.setChecked(True)
        
        # 中间：图片
        self.imgLabel = QLabel(self)
        self.imgLabel.setPixmap(pixmap)
        self.imgLabel.setScaledContents(False) # 保持比例，由Worker调整
        self.imgLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.imgLabel.setFixedWidth(200)
        
        # 右侧：信息
        self.infoLayout = QVBoxLayout()
        fname = url.split("?")[0].split("/")[-1][-8:]
        self.nameLabel = StrongBodyLabel(f"...{fname}", self)
        self.tagLabel = CaptionLabel("无水印原图", self)
        self.tagLabel.setStyleSheet("color: green;")
        
        self.infoLayout.addStretch(1)
        self.infoLayout.addWidget(self.nameLabel)
        self.infoLayout.addWidget(self.tagLabel)
        self.infoLayout.addStretch(1)

        self.hLayout.addWidget(self.check)
        self.hLayout.addWidget(self.imgLabel)
        self.hLayout.addLayout(self.infoLayout)

class HomeInterface(QWidget):
    """首页 - 豆包下载器核心界面"""
    
    launch_finished = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("homeInterface")
        
        # 布局：左侧控制区，右侧画廊区
        self.mainLayout = QHBoxLayout(self)
        self.mainLayout.setContentsMargins(10, 10, 10, 10)
        
        # === 左侧面板 ===
        self.leftFrame = CardWidget(self)
        self.leftFrame.setFixedWidth(300)
        self.leftLayout = QVBoxLayout(self.leftFrame)
        self.leftLayout.setContentsMargins(20, 20, 20, 20)
        self.leftLayout.setSpacing(15)

        self.leftLayout.addWidget(TitleLabel("控制面板", self))
        
        # 1. 启动
        self.leftLayout.addWidget(StrongBodyLabel("步骤 1: 环境", self))
        self.btnLaunch = PrimaryPushButton(FIF.SEARCH, "启动浏览器", self)
        self.btnLaunch.clicked.connect(self.launch_browser)
        self.leftLayout.addWidget(self.btnLaunch)

        # 2. 监听
        self.leftLayout.addWidget(StrongBodyLabel("步骤 2: 捕获", self))
        self.checkMonitor = CheckBox("开启实时监听", self)
        self.checkMonitor.setEnabled(False)
        self.checkMonitor.stateChanged.connect(self.toggle_monitor)
        self.leftLayout.addWidget(self.checkMonitor)

        # 日志区
        self.logArea = TextEdit(self)
        self.logArea.setReadOnly(True)
        self.logArea.setFixedHeight(150)
        self.leftLayout.addWidget(self.logArea)

        # 3. 下载
        self.leftLayout.addWidget(StrongBodyLabel("步骤 3: 操作", self))
        self.btnLayout = QHBoxLayout()
        self.btnSelAll = PushButton("全选", self)
        self.btnDeselAll = PushButton("全不选", self)
        self.btnSelAll.clicked.connect(self.select_all)
        self.btnDeselAll.clicked.connect(self.deselect_all)
        self.btnLayout.addWidget(self.btnSelAll)
        self.btnLayout.addWidget(self.btnDeselAll)
        self.leftLayout.addLayout(self.btnLayout)
        
        self.btnDownload = PrimaryPushButton(FIF.DOWNLOAD, "下载选中图片", self)
        self.btnDownload.clicked.connect(self.download_selected)
        self.leftLayout.addWidget(self.btnDownload)
        
        self.leftLayout.addStretch(1)

        # === 右侧滚动区域 (画廊) ===
        self.scroll = ScrollArea(self)
        self.scroll.setStyleSheet("ScrollArea{background: transparent; border: none}")
        self.galleryWidget = QWidget()
        self.scroll.setWidget(self.galleryWidget)
        self.scroll.setWidgetResizable(True)
        
        self.galleryLayout = QVBoxLayout(self.galleryWidget)
        self.galleryLayout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.galleryLayout.setSpacing(10)

        self.mainLayout.addWidget(self.leftFrame)
        self.mainLayout.addWidget(self.scroll, 1)

        # === 逻辑处理 ===
        self.browser_worker = BrowserWorker()
        self.browser_worker.log_signal.connect(self.append_log)
        self.browser_worker.new_image_signal.connect(self.on_new_image_found)
        self.browser_worker.browser_closed_signal.connect(self.on_browser_closed)
        self.browser_worker.start()

        self.image_cards = []
        self.launch_finished.connect(self.on_launch_finished)

    def append_log(self, text):
        self.logArea.append(f"> {text}")

    def launch_browser(self):
        self.btnLaunch.setEnabled(False)
        threading.Thread(target=self._launch_thread, daemon=True).start()

    def _launch_thread(self):
        self.browser_worker.launch_browser()
        self.launch_finished.emit()

    def on_launch_finished(self):
        self.checkMonitor.setEnabled(True)
        if not self.browser_worker.driver:
             self.btnLaunch.setEnabled(True)

    def toggle_monitor(self, state):
        enable = (state == 2)
        self.browser_worker.set_monitoring(enable)

    def on_browser_closed(self):
        self.checkMonitor.setChecked(False)
        self.checkMonitor.setEnabled(False)
        self.btnLaunch.setEnabled(True)

    def on_new_image_found(self, url):
        self.append_log("捕获新图片，正在加载预览...")
        loader = ThumbnailWorker(url)
        loader.loaded.connect(self.add_image_card)
        loader.start()
        setattr(self, f"loader_{time.time()}", loader)

    def add_image_card(self, url, pixmap):
        card = ImageCard(url, pixmap, self.galleryWidget)
        self.galleryLayout.insertWidget(0, card)
        self.image_cards.append(card)
        self.append_log("预览图加载完成")

    def select_all(self):
        for card in self.image_cards:
            card.check.setChecked(True)

    def deselect_all(self):
        for card in self.image_cards:
            card.check.setChecked(False)

    def download_selected(self):
        to_download = [c for c in self.image_cards if c.check.isChecked()]
        if not to_download:
            InfoBar.warning("提示", "请先勾选图片", parent=self)
            return

        count = 0
        self.append_log(f"开始下载 {len(to_download)} 张图片...")
        
        for card in to_download:
            try:
                timestamp = int(time.time() * 1000)
                filename = f"doubao_{timestamp}_{count}.png"
                path = os.path.join(SAVE_DIR, filename)
                
                resp = requests.get(card.url)
                with open(path, 'wb') as f:
                    f.write(resp.content)
                
                card.setStyleSheet("ImageCard { background-color: #e8f5e9; border: 1px solid green; }")
                count += 1
                QApplication.processEvents()
            except Exception as e:
                self.append_log(f"下载失败: {e}")

        InfoBar.success("完成", f"成功保存 {count} 张图片到 {SAVE_DIR}", parent=self)
        self.append_log("批量下载完成")

class PersonalInterface(ScrollArea):
    """账户中心"""
    login_status_changed = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("personalInterface")
        self.view = QWidget(self)
        self.setWidget(self.view)
        self.setWidgetResizable(True)
        self.view.setObjectName("view")
        self.mainLayout = QVBoxLayout(self.view)
        self.mainLayout.setContentsMargins(36, 20, 36, 36)
        self.mainLayout.setSpacing(20)
        self.setStyleSheet("ScrollArea{background: transparent; border: none} #view{background: transparent}")

        self.mainLayout.addWidget(TitleLabel("账户", self))

        # 个人信息卡片
        self.profileCard = SimpleCardWidget(self.view)
        self.profileLayout = QHBoxLayout(self.profileCard)
        self.profileLayout.setContentsMargins(16, 16, 16, 16)
        
        self.avatar = AvatarWidget(resource_path("logo.ico"), self.profileCard)
        self.avatar.setRadius(32)
        self.avatar.setFixedSize(64, 64)
        
        self.textLayout = QVBoxLayout()
        self.nameLabel = StrongBodyLabel("未登录", self.profileCard)
        self.nameLabel.setStyleSheet("font-size: 16px;")
        self.bioLabel = CaptionLabel("连接 GitHub 同步数据", self.profileCard)
        self.textLayout.addWidget(self.nameLabel)
        self.textLayout.addWidget(self.bioLabel)
        
        self.profileLayout.addWidget(self.avatar)
        self.profileLayout.addLayout(self.textLayout)
        self.profileLayout.addStretch(1)
        
        self.loginBtn = PrimaryPushButton(FIF.GITHUB, " 连接", self.profileCard)
        self.loginBtn.setFixedWidth(100)
        self.loginBtn.clicked.connect(self.start_login)
        self.loginRing = IndeterminateProgressRing(self.profileCard)
        self.loginRing.setFixedSize(20, 20)
        self.loginRing.setVisible(False)
        self.logoutBtn = TransparentToolButton(FIF.CLOSE, self.profileCard)
        self.logoutBtn.setVisible(False)
        self.logoutBtn.clicked.connect(self.logout)
        
        self.profileLayout.addWidget(self.loginBtn)
        self.profileLayout.addWidget(self.loginRing)
        self.profileLayout.addWidget(self.logoutBtn)
        self.mainLayout.addWidget(self.profileCard)

        # 数据详情
        self.detailGroup = SettingCardGroup("详细信息", self.view)
        self.idCard = SettingCard(FIF.INFO, "GitHub ID", "未连接", self.detailGroup)
        self.repoCard = SettingCard(FIF.FOLDER, "公开仓库", "0", self.detailGroup)
        self.detailGroup.addSettingCard(self.idCard)
        self.detailGroup.addSettingCard(self.repoCard)
        self.mainLayout.addWidget(self.detailGroup)

        self.mainLayout.addStretch(1)
        self.check_persistence()

    def check_persistence(self):
        config = AuthConfig.load()
        if config and "token" in config:
            self.set_loading(True)
            self.verifyWorker = VerifyWorker(config["token"])
            self.verifyWorker.verify_finished.connect(self.on_verify_finished)
            self.verifyWorker.start()

    def on_verify_finished(self, success, user_data, token):
        self.set_loading(False)
        if success: self.apply_login(user_data, token, silent=True)
        else: AuthConfig.clear(); self.login_status_changed.emit(False)

    def start_login(self):
        self.set_loading(True)
        self.worker = LoginWorker()
        self.worker.login_success.connect(self.apply_login)
        self.worker.login_failed.connect(lambda m: (self.set_loading(False), InfoBar.error("失败", m, parent=self)))
        self.worker.start()

    def apply_login(self, data, token, silent=False):
        self.set_loading(False)
        AuthConfig.save(token, data)
        self.nameLabel.setText(data.get('name') or data.get('login'))
        self.bioLabel.setText(f"GitHub @{data.get('login')}")
        self.idCard.setContent(str(data.get('id', 'Unknown')))
        self.repoCard.setContent(str(data.get('public_repos', 0)))
        self.loginBtn.setVisible(False); self.logoutBtn.setVisible(True)
        if data.get('avatar_url'):
            self.aw = AvatarWorker(data['avatar_url'])
            self.aw.avatar_loaded.connect(self.avatar.setPixmap); self.aw.start()
        if not silent: InfoBar.success("成功", "GitHub 账户已连接", parent=self)
        self.login_status_changed.emit(True)

    def logout(self):
        AuthConfig.clear()
        self.nameLabel.setText("未登录"); self.bioLabel.setText("连接 GitHub 同步数据")
        self.avatar.setPixmap(QPixmap(resource_path("logo.ico")))
        self.loginBtn.setVisible(True); self.logoutBtn.setVisible(False)
        self.idCard.setContent("未连接"); self.repoCard.setContent("0")
        self.login_status_changed.emit(False)
        InfoBar.info("提示", "已注销账户", parent=self)

    def set_loading(self, is_loading):
        self.loginBtn.setVisible(not is_loading)
        self.loginRing.setVisible(is_loading)
        if is_loading: self.loginRing.start()
        else: self.loginRing.stop()

class SettingInterface(ScrollArea):
    """设置页面"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("settingInterface")
        self.view = QWidget(self)
        self.setWidget(self.view)
        self.setWidgetResizable(True)
        self.vBoxLayout = QVBoxLayout(self.view)
        self.vBoxLayout.setContentsMargins(36, 20, 36, 36)
        self.vBoxLayout.setSpacing(20)
        self.view.setObjectName("view")
        self.setStyleSheet("ScrollArea{background: transparent; border: none} #view{background: transparent}")

        self.titleLabel = TitleLabel("设置", self)
        self.vBoxLayout.addWidget(self.titleLabel)

        # 核心功能：个性化设置组
        self.themeGroup = SettingCardGroup("个性化 (需登录解锁)", self.view)
        
        # 1. 应用主题切换
        self.themeCard = SettingCard(FIF.BRUSH, "应用主题", "切换亮色/暗色模式", self.themeGroup)
        self.themeCombo = ComboBox(self.themeCard)
        self.themeCombo.addItems(["亮色", "暗色", "跟随系统"])
        self.themeCombo.setCurrentIndex(2)
        self.themeCombo.currentTextChanged.connect(lambda t: setTheme(Theme.LIGHT if t=="亮色" else Theme.DARK if t=="暗色" else Theme.AUTO))
        self.themeCard.hBoxLayout.addWidget(self.themeCombo, 0, Qt.AlignmentFlag.AlignRight)
        self.themeCard.hBoxLayout.addSpacing(16)

        # 2. 主题颜色切换
        self.colorCard = SettingCard(FIF.PALETTE, "主题颜色", "选择应用强调色", self.themeGroup)
        self.colorCombo = ComboBox(self.colorCard)
        self.colorCombo.addItems(["默认蓝", "清爽绿", "活力橙", "热烈红", "神秘紫"])
        self.colorCombo.currentTextChanged.connect(self.on_color_changed)
        self.colorCard.hBoxLayout.addWidget(self.colorCombo, 0, Qt.AlignmentFlag.AlignRight)
        self.colorCard.hBoxLayout.addSpacing(16)
        
        self.themeGroup.addSettingCard(self.themeCard)
        self.themeGroup.addSettingCard(self.colorCard)
        self.vBoxLayout.addWidget(self.themeGroup)
        
        # === 新增：关于信息 ===
        self.aboutGroup = SettingCardGroup("关于", self.view)
        self.contactCard = SettingCard(FIF.PEOPLE, "交流反馈", "加入官方QQ交流群", self.aboutGroup)
        
        # 使用标签显示QQ群号，支持复制（Label可选属性）
        self.qqLabel = BodyLabel("1026163188", self.contactCard)
        self.qqLabel.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.contactCard.hBoxLayout.addWidget(self.qqLabel, 0, Qt.AlignmentFlag.AlignRight)
        self.contactCard.hBoxLayout.addSpacing(16)
        
        self.aboutGroup.addSettingCard(self.contactCard)
        self.vBoxLayout.addWidget(self.aboutGroup)

        self.vBoxLayout.addStretch(1)
        
        self.set_enable_status(False)

    def set_enable_status(self, is_logged_in):
        self.themeGroup.setEnabled(is_logged_in)
        status_text = "个性化" if is_logged_in else "个性化 (请先在账户中心登录解锁)"
        self.themeGroup.titleLabel.setText(status_text)
        opacity = QGraphicsOpacityEffect(self)
        opacity.setOpacity(1.0 if is_logged_in else 0.5)
        self.themeGroup.setGraphicsEffect(opacity)

    def on_color_changed(self, text):
        colors = {"默认蓝": "#009faa", "清爽绿": "#00995e", "活力橙": "#FF8C00", "热烈红": "#E74C3C", "神秘紫": "#9B59B6"}
        if text in colors:
            setThemeColor(colors[text])
            qconfig.save()

# ==========================================================
# 5. 主窗口
# ==========================================================
class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("豆包图片下载器")
        icon_path = resource_path("logo.ico")
        if os.path.exists(icon_path): self.setWindowIcon(QIcon(icon_path))
        self.resize(1100, 750)
        
        self.home = HomeInterface(self)
        self.person = PersonalInterface(self)
        self.setting = SettingInterface(self)
        
        self.person.login_status_changed.connect(self.setting.set_enable_status)
        
        self.addSubInterface(self.home, FIF.HOME, "主页")
        self.addSubInterface(self.person, FIF.PEOPLE, "账户中心")
        self.addSubInterface(self.setting, FIF.SETTING, "设置", position=Qt.AlignmentFlag.AlignBottom)
        
        config = AuthConfig.load()
        self.setting.set_enable_status(config is not None)

    def closeEvent(self, event):
        self.home.browser_worker.stop()
        super().closeEvent(event)

if __name__ == '__main__':
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    
    font_name = "Microsoft YaHei UI" if sys.platform == "win32" else "Segoe UI"
    app.setFont(QFont(font_name, 10))
    
    w = MainWindow()
    w.show()
    sys.exit(app.exec())
