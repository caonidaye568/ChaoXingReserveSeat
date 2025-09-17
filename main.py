import json
import time
import argparse
import os
import logging
import datetime
import threading
import copy
from queue import Queue
from concurrent.futures import ThreadPoolExecutor

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

from utils import reserve, get_user_credentials

# --- 时间处理辅助函数 ---
def get_now(action=False):
    """根据运行环境获取当前时间（统一为北京时间）"""
    if action:
        # 在GitHub Action (UTC) 环境下，加上8小时得到北京时间
        return datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    else:
        # 在本地环境，直接获取本地时间
        return datetime.datetime.now()

def get_current_dayofweek(action=False):
    """根据运行环境获取今天是周几（统一为北京时间）"""
    return get_now(action).strftime("%A")

# --- 全局配置 ---
RESERVE_TARGET_TIME = "18:31:00"  # 目标预约时间 (例如 "18:21:00")
ENABLE_SLIDER = True             # 启用滑块验证
RESERVE_NEXT_DAY = False          # 预约明天
CAPTCHA_POOL_SIZE = 10           # 验证码池大小
PRELOAD_START_SECONDS = 8        # 提前多少秒开始预加载验证码

class CaptchaPool:
    """
    终极版验证码池:
    - 在主session上并行预加载，将成功结果存入队列.
    - 解决验证码获取缓慢的核心瓶颈.
    """
    def __init__(self, base_session, pool_size):
        self.base_session = base_session
        self.pool_size = pool_size
        self.captcha_queue = Queue()
        self.is_active = False
        self.preload_threads = []

    def _preload_worker(self):
        """单个预加载线程的工作内容"""
        while self.is_active and self.captcha_queue.qsize() < self.pool_size:
            try:
                session_copy = copy.deepcopy(self.base_session)
                captcha = session_copy.resolve_captcha()
                if captcha:
                    self.captcha_queue.put(captcha)
                    logging.info(f"✅ 验证码预加载成功，当前池大小: {self.captcha_queue.qsize()}")
                else:
                    time.sleep(0.5)
            except Exception as e:
                logging.warning(f"⚠️ 验证码预加载线程异常: {e}")
                time.sleep(1)

    def start(self):
        """启动验证码池的并行预加载"""
        if self.is_active:
            return
        logging.info(f"🔄 启动验证码池预加载，目标数量: {self.pool_size}")
        self.is_active = True
        for _ in range(self.pool_size // 2):
            thread = threading.Thread(target=self._preload_worker, daemon=True)
            thread.start()
            self.preload_threads.append(thread)

    def stop(self):
        """停止所有预加载活动"""
        self.is_active = False
        logging.info("🛑 验证码池停止预加载.")

    def get_captcha(self):
        """从池中获取一个验证码"""
        if not self.captcha_queue.empty():
            return self.captcha_queue.get_nowait()
        else:
            logging.warning("⚠️ 验证码池为空！正在紧急生成...")
            return self.base_session.resolve_captcha()

def lightning_submit_worker(base_session, times, roomid, seatid, captcha, action):
    """
    闪电突袭工作线程: 任务极度简化，只负责光速获取Token和提交.
    """
    start_time = time.time()
    session_copy = copy.deepcopy(base_session)

    try:
        token, value = session_copy._get_page_token(
            session_copy.url.format(roomid, seatid), require_value=True
        )
        if not token:
            logging.error(f"💥 (线程 {times[0]}-{times[1]}) 紧急获取Token失败!")
            return False

        success = session_copy.get_submit(
            session_copy.submit_url,
            times=times,
            token=token,
            roomid=roomid,
            seatid=seatid,
            captcha=captcha,
            action=action,
            value=value,
        )
        
        total_time = time.time() - start_time
        logging.info(f"⚡ (线程 {times[0]}-{times[1]}) 提交完成! 耗时: {total_time:.2f}s")
        return success
        
    except Exception as e:
        total_time = time.time() - start_time
        logging.error(f"💥 (线程 {times[0]}-{times[1]}) 提交时异常: {e}，耗时: {total_time:.2f}s")
        return False

def start_lightning_reservation(user_info, base_session, captcha_pool, action):
    """
    总指挥: 协调资源，发起闪电预约.
    """
    username = user_info['username']
    times = user_info['times']
    roomid = user_info['roomid']
    seatid = user_info['seatid']
    
    logging.info(f"🚀 用户 {username} 所有线程准备就绪, 开始闪电突袭!")
    
    time_slots = times if isinstance(times[0], list) else [times]
    
    with ThreadPoolExecutor(max_workers=len(time_slots)) as executor:
        futures = []
        for slot in time_slots:
            try:
                captcha = captcha_pool.get_captcha()
                future = executor.submit(lightning_submit_worker, base_session, slot, roomid, seatid[0], captcha, action)
                futures.append(future)
            except Exception as e:
                logging.error(f"为时间段 {slot} 分配任务时出错: {e}")

        successful_slots = [time_slots[i] for i, f in enumerate(futures) if f.done() and f.result()]
        
        if successful_slots:
            logging.info(f"🎉 胜利! 用户 {username} 成功预约 {len(successful_slots)} 个时间段: {successful_slots}")
        else:
            logging.warning(f"😞 任务结束. 用户 {username} 所有时间段预约均失败.")

def pre_login_user(user, usernames, passwords, action, index):
    """登录单个用户并返回session对象"""
    username, password, _, roomid, seatid, daysofweek = user.values()
    
    if action:
        username, password = usernames.split(",")[index], passwords.split(",")[index]
    
    user['username'] = username

    if get_current_dayofweek(action) not in daysofweek:
        logging.info(f"用户 {username}: 今天不是预约日, 跳过.")
        return None

    logging.info(f"User {username}: 提前登录中...")
    s = reserve(enable_slider=ENABLE_SLIDER, reserve_next_day=RESERVE_NEXT_DAY)
    s.get_login_status()
    login_success, msg = s.login(username, password)
    
    if login_success:
        s.requests.headers.update({"Host": "office.chaoxing.com"})
        try:
            logging.info(f"🔥 用户 {username}: 开始Session预热...")
            s.requests.get(f"https://office.chaoxing.com/front/third/apps/seat/code?id={roomid}&seatNum={seatid[0]}", verify=False)
            logging.info(f"✅ 用户 {username}: Session预热完成.")
        except Exception as e:
            logging.warning(f"⚠️ 用户 {username} Session预热失败: {e}")
        return s
    else:
        logging.error(f"❌ 用户 {username} 登录失败: {msg}")
        return None

def main(users, action=False):
    logging.info(f"程序启动 (action={'on' if action else 'off'})")
    
    usernames, passwords = get_user_credentials(action) if action else (None, None)

    user_sessions = [pre_login_user(u, usernames, passwords, action, i) for i, u in enumerate(users)]

    captcha_pools = [CaptchaPool(s, CAPTCHA_POOL_SIZE) if s else None for s in user_sessions]
    
    # --- 核心时间计算逻辑修正 ---
    now_dt = get_now(action)
    target_dt = now_dt.replace(
        hour=int(RESERVE_TARGET_TIME.split(":")[0]),
        minute=int(RESERVE_TARGET_TIME.split(":")[1]),
        second=int(RESERVE_TARGET_TIME.split(":")[2]),
        microsecond=0
    )
    if target_dt <= now_dt: # 如果目标时间已过，则设为明天
        target_dt += datetime.timedelta(days=1)
    
    preload_start_dt = target_dt - datetime.timedelta(seconds=PRELOAD_START_SECONDS)
    
    now_dt = get_now(action) # 再次获取当前时间，以防万一
    if now_dt < preload_start_dt:
        wait_seconds = (preload_start_dt - now_dt).total_seconds()
        if wait_seconds > 0:
            logging.info(f"将在 {wait_seconds:.1f} 秒后开始预加载验证码...")
            time.sleep(wait_seconds)
    
    for pool in captcha_pools:
        if pool:
            pool.start()

    now_dt = get_now(action)
    if now_dt < target_dt:
        wait_seconds = (target_dt - now_dt).total_seconds()
        if wait_seconds > 0:
            logging.info(f"距离目标时间 {RESERVE_TARGET_TIME} 还剩 {wait_seconds:.1f} 秒, 精准等待中...")
            time.sleep(wait_seconds)
    
    while get_now(action) < target_dt:
        time.sleep(0.001)

    logging.info(f"到达目标时间 {RESERVE_TARGET_TIME}, 所有用户总攻开始!")

    for i, session in enumerate(user_sessions):
        if session:
            start_lightning_reservation(users[i], session, captcha_pools[i], action)

    for pool in captcha_pools:
        if pool:
            pool.stop()

if __name__ == "__main__":
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    parser = argparse.ArgumentParser(prog="Chao Xing seat auto reserve")
    parser.add_argument("-u", "--user", default=config_path, help="user config file")
    parser.add_argument("-m", "--method", default="reserve", choices=["reserve"], help="run method")
    parser.add_argument("-a", "--action", action="store_true", help="enable for github action")
    args = parser.parse_args()
    
    # 全局修改 RESERVE_TARGET_TIME
    with open(args.user, "r+") as data:
        config = json.load(data)
        usersdata = config["reserve"]
        if "target_time" in config:
            RESERVE_TARGET_TIME = config["target_time"]
            logging.info(f"已从config.json加载预约时间: {RESERVE_TARGET_TIME}")
        
    main(usersdata, args.action)
