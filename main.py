import json
import time
import argparse
import os
import logging
import datetime
import threading
from queue import Queue, LifoQueue
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

SLEEPTIME = 0.02
RESERVE_TARGET_TIME = "16:09:00"  # 【重要】请根据你学校的实际情况修改
ENABLE_SLIDER = True
MAX_ATTEMPT = 1
RESERVE_NEXT_DAY = False # 【重要】如果预约第二天，请改为 True
CAPTCHA_POOL_SIZE = 5
CAPTCHA_PRELOAD_TIME = 5  # 验证码池提前启动时间
TOKEN_POOL_SIZE = 2
TOKEN_REFRESH_TIME = 2 # 【新】Token池在目标时间前2秒开始刷新

class TokenPool:
    """Token缓存池 - 使用后进先出队列"""
    def __init__(self, session, roomid, seatid, pool_size=TOKEN_POOL_SIZE):
        self.session = session
        self.roomid = roomid
        self.seatid = seatid
        self.pool_size = pool_size
        self.token_queue = LifoQueue()
        self.is_active = True
        self.lock = threading.Lock()
        
    def start_preloading(self):
        """开始预加载token"""
        logging.info(f"🔄 开始快速刷新Token池，目标数量: {self.pool_size}")
        
        def preload_worker():
            while self.is_active and self.token_queue.qsize() < self.pool_size:
                try:
                    token, value = self.session._get_page_token(
                        self.session.url.format(self.roomid, self.seatid), require_value=True
                    )
                    if token:
                        self.token_queue.put((token, value))
                        logging.info(f"✅ Token刷新成功: {token[:16]}..., 当前池大小: {self.token_queue.qsize()}")
                    time.sleep(0.1)
                except Exception as e:
                    logging.warning(f"⚠️ Token刷新失败: {e}")
                    time.sleep(0.5)
        
        thread = threading.Thread(target=preload_worker, daemon=True)
        thread.start()
    
    def get_token(self):
        """获取一个token"""
        if not self.token_queue.empty():
            token_data = self.token_queue.get()
            logging.info(f"📋 从Token池中获取最新Token，剩余: {self.token_queue.qsize()}")
            return token_data
        else:
            logging.warning("⚠️ Token池为空，临时生成Token")
            return self.session._get_page_token(
                self.session.url.format(self.roomid, self.seatid), require_value=True
            )
    
    def stop(self):
        """停止预加载"""
        self.is_active = False

class CaptchaPool:
    """验证码缓存池"""
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
                            consecutive_failures = 0
                        else:
                            consecutive_failures += 1
                    time.sleep(0.5)
                except Exception as e:
                    consecutive_failures += 1
                    logging.warning(f"⚠️ 验证码预加载失败: {e}")
                    time.sleep(1)
        
        self._worker_thread = threading.Thread(target=preload_worker, daemon=True)
        self._worker_thread.start()
    
    def get_captcha(self):
        """获取一个验证码"""
        if not self.captcha_queue.empty():
            captcha = self.captcha_queue.get()
            logging.info(f"📋 从验证码池中获取验证码，剩余: {self.captcha_queue.qsize()}")
            return captcha
        else:
            logging.warning("⚠️ 验证码池为空，临时生成验证码")
            return self.session.resolve_captcha()
    
    def stop(self):
        """停止预加载"""
        self.is_active = False

def warm_up_session(session, roomid, seatid):
    """Session预热"""
    try:
        logging.info("🔥 开始Session预热...")
        session.requests.get(f"https://office.chaoxing.com/front/third/apps/seat/code?id={roomid}&seatNum={seatid[0]}", verify=False)
        logging.info("✅ Session预热完成")
    except Exception as e:
        logging.warning(f"⚠️ Session预热失败: {e}")

def ultra_fast_submit(session, times, roomid, seatid, captcha_pool, token_pool, action):
    """使用双池进行快速提交"""
    start_time = time.time()
    
    try:
        # 1. 从Token池获取最新Token
        token_start = time.time()
        token, value = token_pool.get_token()
        token_time = time.time() - token_start
        if not token:
            logging.error("❌ 从池中获取Token失败")
            return False

        # 2. 从验证码池获取验证码
        captcha_start = time.time()
        captcha = captcha_pool.get_captcha()
        captcha_time = time.time() - captcha_start
        if not captcha:
            logging.error("❌ 从池中获取验证码失败")
            return False
            
        logging.info(f"⚡ 准备完成: (Token耗时:{token_time:.2f}s, 验证码耗时:{captcha_time:.2f}s)")
        
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
    """提前登录所有用户并创建资源池"""
    logged_sessions = []
    captcha_pools = []
    token_pools = []
    current_dayofweek = get_current_dayofweek(action)
    
    for index, user in enumerate(users):
        username, password, times, roomid, seatid, daysofweek = user.values()
        if action:
            username, password = (usernames.split(",")[index], passwords.split(",")[index])
        
        if current_dayofweek not in daysofweek:
            logging.info(f"User {username}: Today is not in {daysofweek}, skipping.")
            logged_sessions.append(None); captcha_pools.append(None); token_pools.append(None)
            continue
            
        logging.info(f"User {username}: 提前登录中...")
        
        s = reserve(
            sleep_time=SLEEPTIME, max_attempt=MAX_ATTEMPT,
            enable_slider=ENABLE_SLIDER, reserve_next_day=RESERVE_NEXT_DAY,
        )
        s.get_login_status()
        login_success, msg = s.login(username, password)
        
        if login_success:
            s.requests.headers.update({"Host": "office.chaoxing.com"})
            import requests
            adapter = requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=20, pool_block=False)
            s.requests.mount('http://', adapter); s.requests.mount('https://', adapter)
            
            warm_up_session(s, roomid, seatid)
            
            captcha_pool = CaptchaPool(s, CAPTCHA_POOL_SIZE)
            token_pool = TokenPool(s, roomid, seatid[0], TOKEN_POOL_SIZE)
            
            logged_sessions.append(s)
            captcha_pools.append(captcha_pool)
            token_pools.append(token_pool)
        else:
            logging.error(f"User {username} login failed: {msg}")
            logged_sessions.append(None); captcha_pools.append(None); token_pools.append(None)
    
    return logged_sessions, captcha_pools, token_pools

# 【修正】增加 action 参数
def wait_for_time(target_dt, message, action):
    """等待到指定datetime对象的时间"""
    if action:
        now_dt = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    else:
        now_dt = datetime.datetime.now()

    wait_seconds = (target_dt - now_dt).total_seconds()
    if wait_seconds > 0:
        # 【修正】将 CAPTCHA_PRELOAD_TIME 替换为 TOKEN_REFRESH_TIME 来适配两种消息
        log_message = message.format(wait_seconds, CAPTCHA_PRELOAD_TIME if '验证码' in message else TOKEN_REFRESH_TIME)
        logging.info(log_message)
        time.sleep(wait_seconds)

def start_reservation_ultra_fast(users, logged_sessions, captcha_pools, token_pools, action):
    """开始超快速预约"""
    current_dayofweek = get_current_dayofweek(action)
    
    for index, user in enumerate(users):
        username = user.get("username", "default_user")
        times, roomid, seatid, daysofweek = user.get("times"), user.get("roomid"), user.get("seatid"), user.get("daysofweek")
        
        if current_dayofweek not in daysofweek: continue
        
        s, captcha_pool, token_pool = logged_sessions[index], captcha_pools[index], token_pools[index]
        if s is None or captcha_pool is None or token_pool is None: continue
        
        logging.info(f"🚀 开始超快速预约 - 用户 {username}")
        
        time_slots = times if isinstance(times[0], list) else [times]
        
        for i, time_slot in enumerate(time_slots):
            logging.info(f"⚡ 预约时间段 {i+1}/{len(time_slots)}: {time_slot}")
            
            success = ultra_fast_submit(
                s, time_slot, roomid, seatid[0], captcha_pool, token_pool, action
            )
            
            if success: logging.info(f"✅ 时间段 {time_slot} 预约成功！")
            else: logging.warning(f"❌ 时间段 {time_slot} 预约失败！")

            if i < len(time_slots) - 1: time.sleep(0.1)
        
        captcha_pool.stop(); token_pool.stop()

def main(users, action=False):
    logging.info(f"程序启动，立即登录 (action={'on' if action else 'off'})")
    
    usernames, passwords = (get_user_credentials(action) if action else (None, None))
    
    logged_sessions, captcha_pools, token_pools = pre_login_users(users, usernames, passwords, action)
    
    if action:
        current_dt = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    else:
        current_dt = datetime.datetime.now()

    target_h, target_m, target_s = map(int, RESERVE_TARGET_TIME.split(':'))
    target_dt = current_dt.replace(hour=target_h, minute=target_m, second=target_s, microsecond=0)
    if target_dt <= current_dt:
        target_dt += datetime.timedelta(days=1)

    # 1. 计算并等待到验证码预加载时间
    captcha_start_dt = target_dt - datetime.timedelta(seconds=CAPTCHA_PRELOAD_TIME)
    # 【修正】传入 action
    wait_for_time(captcha_start_dt, "将在 {:.1f} 秒后启动验证码池 (目标时间前{}s)", action)
    for captcha_pool in captcha_pools:
        if captcha_pool: captcha_pool.start_preloading()
    logging.info("✅ 验证码池已启动")

    # 2. 计算并等待到Token刷新时间
    token_start_dt = target_dt - datetime.timedelta(seconds=TOKEN_REFRESH_TIME)
    # 【修正】传入 action
    wait_for_time(token_start_dt, "将在 {:.1f} 秒后刷新Token池 (目标时间前{}s)", action)
    for token_pool in token_pools:
        if token_pool: token_pool.start_preloading()
    logging.info("✅ Token池已开始最后冲刺刷新")

    # 3. 精准等待到目标时间
    # 【修正】传入 action
    wait_for_time(target_dt, "将在 {:.1f} 秒后开始预约", action)
    logging.info(f"到达目标时间 {RESERVE_TARGET_TIME}（北京时间），开始预约")

    start_reservation_ultra_fast(users, logged_sessions, captcha_pools, token_pools, action)

if __name__ == "__main__":
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    parser = argparse.ArgumentParser(prog="Chao Xing seat auto reserve")
    parser.add_argument("-u", "--user", default=config_path, help="user config file")
    parser.add_argument(
        "-m", "--method", default="reserve", choices=["reserve", "debug", "room"], help="for debug",
    )
    parser.add_argument(
        "-a", "--action", action="store_true", help="use --action to enable in github action",
    )
    args = parser.parse_args()
    func_dict = {"reserve": main} # debug和room模式暂不适配新逻辑
    with open(args.user, "r+") as data:
        usersdata = json.load(data)["reserve"]
    func_dict[args.method](usersdata, args.action)
