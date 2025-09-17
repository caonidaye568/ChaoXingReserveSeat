import json
import time
import argparse
import os
import logging
import datetime
import threading
import copy
from queue import Queue
from concurrent.futures import ThreadPoolExecutor, wait

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

from utils import reserve, get_user_credentials

# --- 时间处理辅助函数 ---
def get_now(action=False):
    """根据运行环境获取当前时间（统一为北京时间）"""
    if action:
        return datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    else:
        return datetime.datetime.now()

def get_current_dayofweek(action=False):
    """根据运行环境获取今天是周几（统一为北京时间）"""
    return get_now(action).strftime("%A")

# --- 全局配置 ---
RESERVE_TARGET_TIME = "18:35:00"
ENABLE_SLIDER = True
RESERVE_NEXT_DAY = False
POOL_SIZE = 15  # 验证码和Token池的大小
PRELOAD_START_SECONDS = 8 # 提前多少秒开始预加载

class ResourcePool:
    """通用资源池，用于预加载验证码和Token"""
    def __init__(self, name, base_session, pool_size, target_func):
        self.name = name
        self.base_session = base_session
        self.pool_size = pool_size
        self.queue = Queue()
        self.is_active = False
        self.target_func = target_func

    def _preload_worker(self):
        while self.is_active and self.queue.qsize() < self.pool_size:
            try:
                session_copy = copy.deepcopy(self.base_session)
                resource = self.target_func(session_copy)
                if resource:
                    self.queue.put(resource)
                    logging.info(f"✅ {self.name}预加载成功，当前池大小: {self.queue.qsize()}")
                else:
                    time.sleep(0.3)
            except Exception as e:
                logging.warning(f"⚠️ {self.name}预加载线程异常: {e}")
                time.sleep(0.5)

    def start(self):
        if self.is_active: return
        logging.info(f"🔄 启动{self.name}池预加载，目标数量: {self.pool_size}")
        self.is_active = True
        # 使用多个线程并行预加载
        for _ in range(self.pool_size // 2):
            thread = threading.Thread(target=self._preload_worker, daemon=True)
            thread.start()

    def stop(self):
        self.is_active = False
        logging.info(f"🛑 {self.name}池停止预加载.")

    def get(self):
        if not self.queue.empty():
            return self.queue.get_nowait()
        else:
            logging.warning(f"⚠️ {self.name}池为空！正在紧急生成...")
            return self.target_func(self.base_session)

def lightning_submit_worker(base_session, times, roomid, seatid, captcha, token_data, action):
    """闪电突袭工作线程: 任务简化到只剩提交"""
    session_copy = copy.deepcopy(base_session)
    token, value = token_data

    try:
        success = session_copy.get_submit(
            session_copy.submit_url, times=times, token=token, roomid=roomid,
            seatid=seatid, captcha=captcha, action=action, value=value,
        )
        return success
    except Exception as e:
        logging.error(f"💥 (线程 {times[0]}-{times[1]}) 提交时异常: {e}")
        return False

def start_lightning_reservation(user_info, base_session, captcha_pool, token_pool, action):
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
                captcha = captcha_pool.get()
                token_data = token_pool.get()
                future = executor.submit(lightning_submit_worker, base_session, slot, roomid, seatid[0], captcha, token_data, action)
                futures.append(future)
            except Exception as e:
                logging.error(f"为时间段 {slot} 分配任务时出错: {e}")

        # 【BUG修复】使用wait确保所有任务都完成后再继续
        wait(futures)

        successful_slots = [time_slots[i] for i, f in enumerate(futures) if f.done() and f.result()]
        
        if successful_slots:
            logging.info(f"🎉 胜利! 用户 {username} 成功预约 {len(successful_slots)} 个时间段: {successful_slots}")
        else:
            logging.warning(f"😞 任务结束. 用户 {username} 所有时间段预约均失败.")

def pre_login_user(user, usernames, passwords, action, index):
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

    # --- 为每个用户创建并启动双资源池 ---
    captcha_pools = []
    token_pools = []
    for i, session in enumerate(user_sessions):
        if session:
            # 定义获取资源的函数
            get_captcha_func = lambda s: s.resolve_captcha()
            get_token_func = lambda s: s._get_page_token(
                s.url.format(users[i]['roomid'], users[i]['seatid'][0]), require_value=True
            )
            
            captcha_pool = ResourcePool("验证码", session, POOL_SIZE, get_captcha_func)
            token_pool = ResourcePool("Token", session, POOL_SIZE, get_token_func)
            
            captcha_pools.append(captcha_pool)
            token_pools.append(token_pool)
        else:
            captcha_pools.append(None)
            token_pools.append(None)

    # --- 时间计算与等待 ---
    now_dt = get_now(action)
    target_dt = now_dt.replace(
        hour=int(RESERVE_TARGET_TIME.split(":")[0]),
        minute=int(RESERVE_TARGET_TIME.split(":")[1]),
        second=int(RESERVE_TARGET_TIME.split(":")[2]),
        microsecond=0
    )
    if target_dt <= now_dt:
        target_dt += datetime.timedelta(days=1)
    
    preload_start_dt = target_dt - datetime.timedelta(seconds=PRELOAD_START_SECONDS)
    
    now_dt = get_now(action)
    if now_dt < preload_start_dt:
        wait_seconds = (preload_start_dt - now_dt).total_seconds()
        if wait_seconds > 0:
            logging.info(f"将在 {wait_seconds:.1f} 秒后开始预加载...")
            time.sleep(wait_seconds)
    
    # 同时启动所有用户的资源池
    for pool in captcha_pools + token_pools:
        if pool: pool.start()

    now_dt = get_now(action)
    if now_dt < target_dt:
        wait_seconds = (target_dt - now_dt).total_seconds()
        if wait_seconds > 0:
            logging.info(f"距离目标时间 {RESERVE_TARGET_TIME} 还剩 {wait_seconds:.1f} 秒, 精准等待中...")
            time.sleep(wait_seconds)
    
    while get_now(action) < target_dt:
        time.sleep(0.001)

    logging.info(f"到达目标时间 {RESERVE_TARGET_TIME}, 所有用户总攻开始!")

    # 对每个用户发起总攻
    for i, session in enumerate(user_sessions):
        if session:
            start_lightning_reservation(users[i], session, captcha_pools[i], token_pools[i], action)

    # 停止所有资源池
    for pool in captcha_pools + token_pools:
        if pool: pool.stop()

if __name__ == "__main__":
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    parser = argparse.ArgumentParser(prog="Chao Xing seat auto reserve")
    parser.add_argument("-u", "--user", default=config_path, help="user config file")
    parser.add_argument("-m", "--method", default="reserve", choices=["reserve"], help="run method")
    parser.add_argument("-a", "--action", action="store_true", help="enable for github action")
    args = parser.parse_args()
    
    with open(args.user, "r+") as data:
        config = json.load(data)
        usersdata = config["reserve"]
        if "target_time" in config:
            RESERVE_TARGET_TIME = config["target_time"]
            logging.info(f"已从config.json加载预约时间: {RESERVE_TARGET_TIME}")
        
    main(usersdata, args.action)
