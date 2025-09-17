import json
import time
import argparse
import os
import logging
import datetime
import threading
from queue import Queue, LifoQueue # <-- 修改点：额外导入LifoQueue
from concurrent.futures import ThreadPoolExecutor, as_completed

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

from utils import reserve, get_user_credentials

get_current_time = lambda action: (
    (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime("%H:%M:%S")
    if action
    else time.strftime("%H:%M:%S", time.localtime())
)
get_current_dayofweek = lambda action: (
    (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime("%A")
    if action
    else time.strftime("%A", time.localtime())
)

SLEEPTIME = 0.02  # 进一步减少间隔时间
RESERVE_TARGET_TIME = "18:11:00"  # 预约开始的目标时间（北京时间）
ENABLE_SLIDER = True  # 是否有滑块验证
MAX_ATTEMPT = 1  # 减少重试次数，专注速度
RESERVE_NEXT_DAY = False  # 预约明天而不是今天的
CAPTCHA_POOL_SIZE = 5  # 适中的验证码池大小
CAPTCHA_PRELOAD_TIME = 2  # 提前8秒开始预加载验证码
TOKEN_POOL_SIZE = 1  # 减少Token池大小避免过多请求

class TokenPool:
    """Token缓存池"""
    def __init__(self, session, roomid, seatid, pool_size=TOKEN_POOL_SIZE):
        self.session = session
        self.roomid = roomid
        self.seatid = seatid
        self.pool_size = pool_size
        self.token_queue = LifoQueue() # <-- 修改点：使用LifoQueue确保后进先出
        self.is_active = True
        self.lock = threading.Lock()
        
    def start_preloading(self):
        """开始预加载token"""
        logging.info(f"🔄 开始预加载Token池，目标数量: {self.pool_size}")
        
        def preload_worker():
            while self.is_active and self.token_queue.qsize() < self.pool_size:
                try:
                    token, value = self.session._get_page_token(
                        self.session.url.format(self.roomid, self.seatid), require_value=True
                    )
                    if token:
                        self.token_queue.put((token, value))
                        logging.info(f"✅ Token预加载成功: {token[:16]}..., 当前池大小: {self.token_queue.qsize()}")
                    time.sleep(0.1)  # 避免请求过快
                except Exception as e:
                    logging.warning(f"⚠️ Token预加载失败: {e}")
                    time.sleep(0.5)
        
        # 启动预加载线程
        thread = threading.Thread(target=preload_worker, daemon=True)
        thread.start()
    
    def get_token(self):
        """获取一个token"""
        if not self.token_queue.empty():
            return self.token_queue.get()
        else:
            logging.warning("⚠️ Token池为空，临时生成Token")
            return self.session._get_page_token(
                self.session.url.format(self.roomid, self.seatid), require_value=True
            )
    
    def stop(self):
        """停止预加载"""
        self.is_active = False

class CaptchaPool:
    """验证码缓存池 - 单线程优化版本"""
    def __init__(self, session, pool_size=CAPTCHA_POOL_SIZE):
        self.session = session
        self.pool_size = pool_size
        self.captcha_queue = Queue()
        self.is_active = True
        self.lock = threading.Lock()
        self._worker_thread = None
        
    def start_preloading(self):
        """开始预加载验证码"""
        logging.info(f"🔄 开始预加载验证码池，目标数量: {self.pool_size}")
        
        def preload_worker():
            consecutive_failures = 0
            while self.is_active and consecutive_failures < 5:
                try:
                    if self.captcha_queue.qsize() < self.pool_size:
                        captcha = self.session.resolve_captcha()
                        if captcha:
                            self.captcha_queue.put(captcha)
                            logging.info(f"✅ 验证码预加载成功，当前池大小: {self.captcha_queue.qsize()}")
                            consecutive_failures = 0  # 重置失败计数
                        else:
                            consecutive_failures += 1
                    time.sleep(0.5)  # 适当间隔避免请求过快
                except Exception as e:
                    consecutive_failures += 1
                    logging.warning(f"⚠️ 验证码预加载失败: {e}")
                    time.sleep(1)
        
        # 启动单个预加载线程
        self._worker_thread = threading.Thread(target=preload_worker, daemon=True)
        self._worker_thread.start()
    
    def get_captcha(self):
        """获取一个验证码"""
        if not self.captcha_queue.empty():
            captcha = self.captcha_queue.get()
            logging.info(f"📋 从池中获取验证码，剩余: {self.captcha_queue.qsize()}")
            return captcha
        else:
            logging.warning("⚠️ 验证码池为空，临时生成验证码")
            return self.session.resolve_captcha()
    
    def stop(self):
        """停止预加载"""
        self.is_active = False

def warm_up_session(session, roomid, seatid):
    """Session预热 - 增强版"""
    try:
        logging.info("🔥 开始Session预热...")
        
        # 预热请求列表
        warm_up_urls = [
            f"https://office.chaoxing.com/front/third/apps/seat/code?id={roomid}&seatNum={seatid[0]}",
            "https://office.chaoxing.com/data/apps/seat/getusedtimes",
            f"https://office.chaoxing.com/data/apps/seat/room/layout?id={roomid}",
        ]
        
        # 并行预热
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = []
            for url in warm_up_urls:
                future = executor.submit(session.requests.get, url, verify=False)
                futures.append(future)
            
            # 等待所有预热请求完成
            for future in as_completed(futures, timeout=5):
                try:
                    future.result()
                except Exception as e:
                    logging.warning(f"⚠️ 预热请求失败: {e}")
        
        logging.info("✅ Session预热完成")
    except Exception as e:
        logging.warning(f"⚠️ Session预热失败: {e}")

def ultra_fast_submit(session, times, roomid, seatid, captcha_pool, token_pool, action):
    """超快速提交单个预约 - 串行优化版本"""
    start_time = time.time()
    
    try:
        # 1. 快速获取验证码（从池中取） <-- 恢复使用验证码池
        captcha_start = time.time()
        captcha = captcha_pool.get_captcha()
        captcha_time = time.time() - captcha_start
        
        # 2. 快速获取token（从后进先出的池中取）
        token_start = time.time()
        token, value = token_pool.get_token()
        token_time = time.time() - token_start
        
        prep_time = time.time()
        logging.info(f"⚡ 超快速准备完成: token={token[:16]}... (验证码:{captcha_time:.2f}s, token:{token_time:.2f}s)")
        
        # 3. 立即提交
        success = session.get_submit(
            session.submit_url,
            times=times,
            token=token,
            roomid=roomid,
            seatid=seatid,
            captcha=captcha,
            action=action,
            value=value,
        )
        
        total_time = time.time() - start_time
        if success:
            logging.info(f"🎉 预约成功！总耗时: {total_time:.2f}s")
        else:
            logging.warning(f"❌ 预约失败，总耗时: {total_time:.2f}s")
            
        return success
        
    except Exception as e:
        total_time = time.time() - start_time
        logging.error(f"💥 预约异常: {e}，总耗时: {total_time:.2f}s")
        return False

def pre_login_users(users, usernames, passwords, action):
    """提前登录所有用户并预热 - 增强版"""
    logged_sessions = []
    captcha_pools = []
    token_pools = []
    current_dayofweek = get_current_dayofweek(action)
    
    for index, user in enumerate(users):
        username, password, times, roomid, seatid, daysofweek = user.values()
        if action:
            username, password = (
                usernames.split(",")[index],
                passwords.split(",")[index],
            )
        
        if current_dayofweek not in daysofweek:
            logging.info("Today not set to reserve")
            logged_sessions.append(None)
            captcha_pools.append(None)
            token_pools.append(None)
            continue
            
        logging.info(f"User {username}: 提前登录中...")
        
        # 创建预约会话并登录
        s = reserve(
            sleep_time=SLEEPTIME,
            max_attempt=MAX_ATTEMPT,
            enable_slider=ENABLE_SLIDER,
            reserve_next_day=RESERVE_NEXT_DAY,
        )
        s.get_login_status()
        login_success, msg = s.login(username, password)
        
        if login_success:
            s.requests.headers.update({"Host": "office.chaoxing.com"})
            
            # 优化请求配置
            import requests
            s.requests.keep_alive = True
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=10, 
                pool_maxsize=20,
                pool_block=False  # 不阻塞获取连接
            )
            s.requests.mount('http://', adapter)
            s.requests.mount('https://', adapter)
            
            # Session预热
            warm_up_session(s, roomid, seatid)
            
            # 创建验证码池和Token池
            captcha_pool = CaptchaPool(s, CAPTCHA_POOL_SIZE)
            token_pool = TokenPool(s, roomid, seatid[0], TOKEN_POOL_SIZE)
            
            logged_sessions.append(s)
            captcha_pools.append(captcha_pool)
            token_pools.append(token_pool)
        else:
            logging.error(f"User {username} login failed: {msg}")
            logged_sessions.append(None)
            captcha_pools.append(None)
            token_pools.append(None)
    
    return logged_sessions, captcha_pools, token_pools

def wait_for_target_time(target_time, action):
    """等待到达目标时间"""
    current_time = get_current_time(action)
    
    if current_time < target_time:
        # 获取当前北京时间
        if action:
            current_dt = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
        else:
            current_dt = datetime.datetime.now()
            
        # 计算今天的目标时间
        target_dt = current_dt.replace(
            hour=int(target_time.split(":")[0]),
            minute=int(target_time.split(":")[1]),
            second=int(target_time.split(":")[2]),
            microsecond=0
        )
        
        # 如果目标时间已过，则设为明天
        if target_dt <= current_dt:
            target_dt += datetime.timedelta(days=1)
        
        wait_seconds = (target_dt - current_dt).total_seconds()
        logging.info(f"距离目标时间 {target_time}（北京时间）还有 {wait_seconds:.1f} 秒，sleep……")
        
        time.sleep(wait_seconds)
    
    logging.info(f"到达目标时间 {target_time}（北京时间），开始预约")

def start_reservation_ultra_fast(users, logged_sessions, captcha_pools, token_pools, action):
    """开始超快速预约 - 串行版本（避免被检测）"""
    current_dayofweek = get_current_dayofweek(action)
    
    for index, user in enumerate(users):
        username, password, times, roomid, seatid, daysofweek = user.values()
        
        if current_dayofweek not in daysofweek:
            continue
            
        s = logged_sessions[index]
        captcha_pool = captcha_pools[index]
        token_pool = token_pools[index]
        if s is None or captcha_pool is None or token_pool is None:
            continue
        
        logging.info(f"🚀 开始超快速预约 - 用户 {username}")
        
        # 处理时间段
        time_slots = times if isinstance(times[0], list) else [times]
        
        # 串行提交所有时间段（避免被检测）
        for i, time_slot in enumerate(time_slots):
            logging.info(f"⚡ 预约时间段 {i+1}/{len(time_slots)}: {time_slot}")
            
            success = ultra_fast_submit(
                s, time_slot, roomid, seatid[0], captcha_pool, token_pool, action
            )
            
            if success:
                logging.info(f"✅ 时间段 {time_slot} 预约成功！")
            else:
                logging.warning(f"❌ 时间段 {time_slot} 预约失败！")

            # 适当间隔，避免被检测为机器人
            if i < len(time_slots) - 1:
                time.sleep(0.1)  # 100ms间隔
        
        # 停止资源池
        captcha_pool.stop()
        token_pool.stop()

def main(users, action=False):
    current_time = get_current_time(action)
    logging.info(f"程序启动，立即登录 (action={'on' if action else 'off'})")
    
    usernames, passwords = None, None
    if action:
        usernames, passwords = get_user_credentials(action)
    
    # 提前登录所有用户并预热
    logged_sessions, captcha_pools, token_pools = pre_login_users(users, usernames, passwords, action)
    
    # 在目标时间前启动资源池
    if action:
        current_dt = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    else:
        current_dt = datetime.datetime.now()

    target_dt = current_dt.replace(
        hour=int(RESERVE_TARGET_TIME.split(":")[0]),
        minute=int(RESERVE_TARGET_TIME.split(":")[1]),
        second=int(RESERVE_TARGET_TIME.split(":")[2]),
        microsecond=0
    )
    if target_dt <= current_dt:
        target_dt += datetime.timedelta(days=1)

    start_dt = target_dt - datetime.timedelta(seconds=CAPTCHA_PRELOAD_TIME)
    wait_seconds = (start_dt - current_dt).total_seconds()
    if wait_seconds > 0:
        logging.info(f"将在 {wait_seconds:.1f} 秒后启动资源池 (目标时间前{CAPTCHA_PRELOAD_TIME}s)")
        time.sleep(wait_seconds)

    # 启动所有资源池
    for captcha_pool, token_pool in zip(captcha_pools, token_pools):
        if captcha_pool and token_pool:
            captcha_pool.start_preloading()
            token_pool.start_preloading()
    logging.info("✅ 资源池已启动")

    # 等待目标时间
    wait_for_target_time(RESERVE_TARGET_TIME, action)
    
    # 开始超快速预约
    start_reservation_ultra_fast(users, logged_sessions, captcha_pools, token_pools, action)

def debug(users, action=False):
    logging.info(
        f"Global settings: \nSLEEPTIME: {SLEEPTIME}\nRESERVE_TARGET_TIME: {RESERVE_TARGET_TIME}\nENABLE_SLIDER: {ENABLE_SLIDER}\nRESERVE_NEXT_DAY: {RESERVE_NEXT_DAY}\nCAPTCHA_POOL_SIZE: {CAPTCHA_POOL_SIZE}"
    )
    logging.info(f"Debug Mode start! , action {'on' if action else 'off'}")
    
    if action:
        usernames, passwords = get_user_credentials(action)
    
    current_dayofweek = get_current_dayofweek(action)
    for index, user in enumerate(users):
        username, password, times, roomid, seatid, daysofweek = user.values()
        if type(seatid) == str:
            seatid = [seatid]
        if action:
            username, password = (
                usernames.split(",")[index],
                passwords.split(",")[index],
            )
        if current_dayofweek not in daysofweek:
            logging.info("Today not set to reserve")
            continue
        logging.info(f"----------- {username} -- {times} -- {seatid} try -----------")
        s = reserve(
            sleep_time=SLEEPTIME,
            max_attempt=MAX_ATTEMPT,
            enable_slider=ENABLE_SLIDER,
            reserve_next_day=RESERVE_NEXT_DAY,
        )
        s.get_login_status()
        s.login(username, password)
        s.requests.headers.update({"Host": "office.chaoxing.com"})
        
        # 预热并测试超快速提交
        warm_up_session(s, roomid, seatid)
        captcha_pool = CaptchaPool(s, 5)
        token_pool = TokenPool(s, roomid, seatid[0], 3)
        captcha_pool.start_preloading()
        token_pool.start_preloading()
        time.sleep(3)  # 等待资源池预加载
        
        # 测试超快速预约
        if isinstance(times[0], list):
            for i, time_slot in enumerate(times):
                ultra_fast_submit(s, time_slot, roomid, seatid[0], captcha_pool, token_pool, action)
                if i < len(times) - 1:
                    time.sleep(0.2)  # 避免被检测
        else:
            ultra_fast_submit(s, times, roomid, seatid[0], captcha_pool, token_pool, action)
        
        captcha_pool.stop()
        token_pool.stop()
        return

def get_roomid(args1, args2):
    username = input("请输入用户名：")
    password = input("请输入密码：")
    s = reserve(
        sleep_time=SLEEPTIME,
        max_attempt=MAX_ATTEMPT,
        enable_slider=ENABLE_SLIDER,
        reserve_next_day=RESERVE_NEXT_DAY,
    )
    s.get_login_status()
    s.login(username=username, password=password)
    s.requests.headers.update({"Host": "office.chaoxing.com"})
    encode = input("请输入deptldEnc：")
    s.roomid(encode)

if __name__ == "__main__":
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    parser = argparse.ArgumentParser(prog="Chao Xing seat auto reserve")
    parser.add_argument("-u", "--user", default=config_path, help="user config file")
    parser.add_argument(
        "-m",
        "--method",
        default="reserve",
        choices=["reserve", "debug", "room"],
        help="for debug",
    )
    parser.add_argument(
        "-a",
        "--action",
        action="store_true",
        help="use --action to enable in github action",
    )
    args = parser.parse_args()
    func_dict = {"reserve": main, "debug": debug, "room": get_roomid}
    with open(args.user, "r+") as data:
        usersdata = json.load(data)["reserve"]
    func_dict[args.method](usersdata, args.action)
