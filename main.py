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

# --- 全局配置 ---
RESERVE_TARGET_TIME = "18:21:00"  # 目标预约时间
ENABLE_SLIDER = True             # 启用滑块验证
RESERVE_NEXT_DAY = False          # 预约明天
CAPTCHA_POOL_SIZE = 10           # 验证码池大小 (适当增大以提高冗余)
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
                # 使用一个独立的会话副本进行验证码请求，避免互相干扰
                session_copy = copy.deepcopy(self.base_session)
                captcha = session_copy.resolve_captcha()
                if captcha:
                    self.captcha_queue.put(captcha)
                    logging.info(f"✅ 验证码预加载成功，当前池大小: {self.captcha_queue.qsize()}")
                else:
                    # 服务器返回了失败结果，短暂休眠
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
        # 使用多个线程并行预加载，提高效率
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
            logging.warning("⚠️ 验证码池为空！这是不理想的情况，成功率会降低。")
            # 作为备用方案，紧急生成一个 (但这会很慢)
            return self.base_session.resolve_captcha()

def lightning_submit_worker(base_session, times, roomid, seatid, captcha, action):
    """
    闪电突袭工作线程: 任务极度简化，只负责光速获取Token和提交.
    """
    start_time = time.time()
    session_copy = copy.deepcopy(base_session) # 确保会话隔离

    try:
        # 1. 光速获取Token
        token, value = session_copy._get_page_token(
            session_copy.url.format(roomid, seatid), require_value=True
        )
        if not token:
            logging.error(f"💥 (线程 {times[0]}-{times[1]}) 紧急获取Token失败!")
            return False

        # 2. 光速提交
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
                # 从池中为每个任务分配一个预加载好的验证码
                captcha = captcha_pool.get_captcha()
                future = executor.submit(lightning_submit_worker, base_session, slot, roomid, seatid[0], captcha, action)
                futures.append(future)
            except Exception as e:
                logging.error(f"为时间段 {slot} 分配任务时出错: {e}")

        # 结果汇总
        successful_slots = [time_slots[i] for i, f in enumerate(futures) if f.done() and f.result()]
        
        if successful_slots:
            logging.info(f"🎉 胜利! 用户 {username} 成功预约 {len(successful_slots)} 个时间段: {successful_slots}")
        else:
            logging.warning(f"😞 任务结束. 用户 {username} 所有时间段预约均失败.")

def pre_login_user(user, usernames, passwords, action, index):
    """登录单个用户并返回session对象"""
    username, password, _, roomid, seatid, daysofweek = user.values()
    current_dayofweek = get_current_dayofweek(action)

    if action:
        username, password = usernames.split(",")[index], passwords.split(",")[index]
    
    # 将username存回字典，方便后续日志打印
    user['username'] = username

    if current_dayofweek not in daysofweek:
        logging.info(f"用户 {username}: 今天不是预约日, 跳过.")
        return None

    logging.info(f"User {username}: 提前登录中...")
    s = reserve(enable_slider=ENABLE_SLIDER, reserve_next_day=RESERVE_NEXT_DAY)
    s.get_login_status()
    login_success, msg = s.login(username, password)
    
    if login_success:
        s.requests.headers.update({"Host": "office.chaoxing.com"})
        # 预热Session
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

    # 逐个登录所有用户
    user_sessions = [pre_login_user(u, usernames, passwords, action, i) for i, u in enumerate(users)]

    # 为每个成功登录的用户创建并启动验证码池
    captcha_pools = [CaptchaPool(s, CAPTCHA_POOL_SIZE) if s else None for s in user_sessions]
    
    # 计算并等待预加载时间的到来
    target_dt = datetime.datetime.strptime(f"{datetime.date.today()} {RESERVE_TARGET_TIME}", "%Y-%m-%d %H:%M:%S")
    preload_start_dt = target_dt - datetime.timedelta(seconds=PRELOAD_START_SECONDS)
    
    now_dt = datetime.datetime.now()
    if now_dt < preload_start_dt:
        wait_seconds = (preload_start_dt - now_dt).total_seconds()
        logging.info(f"将在 {wait_seconds:.1f} 秒后开始预加载验证码...")
        time.sleep(wait_seconds)
    
    for pool in captcha_pools:
        if pool:
            pool.start()

    # 精准等待目标时间
    now_dt = datetime.datetime.now()
    if now_dt < target_dt:
        wait_seconds = (target_dt - now_dt).total_seconds()
        logging.info(f"距离目标时间 {RESERVE_TARGET_TIME} 还剩 {wait_seconds:.1f} 秒, 精准等待中...")
        time.sleep(max(0, wait_seconds))
    
    while datetime.datetime.now() < target_dt:
        time.sleep(0.001)

    logging.info(f"到达目标时间 {RESERVE_TARGET_TIME}, 所有用户总攻开始!")

    # 对每个用户发起总攻
    for i, session in enumerate(user_sessions):
        if session:
            start_lightning_reservation(users[i], session, captcha_pools[i], action)

    # 停止所有验证码池
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
    
    with open(args.user, "r+") as data:
        usersdata = json.load(data)["reserve"]
        
    main(usersdata, args.action)
