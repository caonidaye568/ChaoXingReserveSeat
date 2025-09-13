import json
import time
import argparse
import os
import logging
import datetime
import threading
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

# ================= 全局参数 =================
SLEEPTIME = 0.1  # 减少间隔时间
RESERVE_TARGET_TIME = "16:45:00"  # 预约开始的目标时间（北京时间）
ENABLE_SLIDER = True  # 是否有滑块验证
MAX_ATTEMPT = 3  # 减少重试次数，专注速度
RESERVE_NEXT_DAY = False  # 预约明天而不是今天的
CAPTCHA_POOL_SIZE = 5  # 验证码池大小
CAPTCHA_PRELOAD_AT = "16:44:00"  # 验证码池启动时间（北京时间）
# ==========================================


class CaptchaPool:
    """验证码缓存池"""
    def __init__(self, session, pool_size=CAPTCHA_POOL_SIZE):
        self.session = session
        self.pool_size = pool_size
        self.captcha_queue = Queue()
        self.is_active = True
        self.lock = threading.Lock()
        
    def start_preloading(self):
        """开始预加载验证码"""
        logging.info(f"🔄 开始预加载验证码池，目标数量: {self.pool_size}")
        
        def preload_worker():
            while self.is_active and self.captcha_queue.qsize() < self.pool_size:
                try:
                    captcha = self.session.resolve_captcha()
                    if captcha:
                        self.captcha_queue.put(captcha)
                        logging.info(f"✅ 验证码预加载成功，当前池大小: {self.captcha_queue.qsize()}")
                    time.sleep(0.5)  # 避免请求过快
                except Exception as e:
                    logging.warning(f"⚠️ 验证码预加载失败: {e}")
                    time.sleep(1)
        
        thread = threading.Thread(target=preload_worker, daemon=True)
        thread.start()
    
    def get_captcha(self):
        """获取一个验证码"""
        if not self.captcha_queue.empty():
            return self.captcha_queue.get()
        else:
            logging.warning("⚠️ 验证码池为空，临时生成验证码")
            return self.session.resolve_captcha()
    
    def stop(self):
        """停止预加载"""
        self.is_active = False


def warm_up_session(session, roomid, seatid):
    """Session预热 - 模拟正常浏览行为"""
    try:
        logging.info("🔥 开始Session预热...")
        session.requests.get(
            f"https://office.chaoxing.com/front/third/apps/seat/code?id={roomid}&seatNum={seatid[0]}", 
            verify=False
        )
        time.sleep(0.5)
        session.requests.get("https://office.chaoxing.com/data/apps/seat/getusedtimes", verify=False)
        time.sleep(0.5)
        logging.info("✅ Session预热完成")
    except Exception as e:
        logging.warning(f"⚠️ Session预热失败: {e}")


def rapid_submit_single(session, times, roomid, seatid, captcha_pool, action):
    """极速提交单个预约"""
    start_time = time.time()
    
    try:
        captcha = captcha_pool.get_captcha()
        captcha_time = time.time()
        
        token, value = session._get_page_token(
            session.url.format(roomid, seatid), require_value=True
        )
        token_time = time.time()
        logging.info(f"⚡ 快速获取token: {token} (耗时: {token_time - captcha_time:.2f}s)")
        
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
    """提前登录所有用户并预热"""
    logged_sessions = []
    captcha_pools = []
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
            continue
            
        logging.info(f"User {username}: 提前登录中...")
        
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
            warm_up_session(s, roomid, seatid)
            
            captcha_pool = CaptchaPool(s, CAPTCHA_POOL_SIZE)
            # ⚠️ 注意：这里不再立刻启动池子
            logged_sessions.append(s)
            captcha_pools.append(captcha_pool)
        else:
            logging.error(f"User {username} login failed: {msg}")
            logged_sessions.append(None)
            captcha_pools.append(None)
    
    return logged_sessions, captcha_pools


def wait_for_target_time(target_time, action):
    """等待到达目标时间"""
    current_time = get_current_time(action)
    if current_time < target_time:
        if action:
            current_dt = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
        else:
            current_dt = datetime.datetime.now()
        target_dt = current_dt.replace(
            hour=int(target_time.split(":")[0]),
            minute=int(target_time.split(":")[1]),
            second=int(target_time.split(":")[2]),
            microsecond=0
        )
        if target_dt <= current_dt:
            target_dt += datetime.timedelta(days=1)
        wait_seconds = (target_dt - current_dt).total_seconds()
        logging.info(f"距离目标时间 {target_time}（北京时间）还有 {wait_seconds:.1f} 秒，sleep……")
        time.sleep(wait_seconds)
    logging.info(f"到达目标时间 {target_time}（北京时间），开始预约")


def wait_until_preload_time(preload_time, action=False):
    """等待到达验证码池预加载时间"""
    current_time = get_current_time(action)
    if current_time < preload_time:
        if action:
            current_dt = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
        else:
            current_dt = datetime.datetime.now()
        target_dt = current_dt.replace(
            hour=int(preload_time.split(":")[0]),
            minute=int(preload_time.split(":")[1]),
            second=int(preload_time.split(":")[2]),
            microsecond=0
        )
        if target_dt <= current_dt:
            target_dt += datetime.timedelta(days=1)
        wait_seconds = (target_dt - current_dt).total_seconds()
        logging.info(f"距离验证码预加载时间 {preload_time}（北京时间）还有 {wait_seconds:.1f} 秒，sleep……")
        time.sleep(wait_seconds)
    logging.info(f"到达 {preload_time}（北京时间），启动验证码池")


def start_reservation_optimized(users, logged_sessions, captcha_pools, action):
    """开始极速预约"""
    current_dayofweek = get_current_dayofweek(action)
    
    for index, user in enumerate(users):
        username, password, times, roomid, seatid, daysofweek = user.values()
        if current_dayofweek not in daysofweek:
            continue
            
        s = logged_sessions[index]
        captcha_pool = captcha_pools[index]
        if s is None or captcha_pool is None:
            continue
        
        logging.info(f"🚀 开始极速预约 - 用户 {username}")
        
        time_slots = times if isinstance(times[0], list) else [times]
        
        for i, time_slot in enumerate(time_slots):
            logging.info(f"⚡ 预约时间段 {i+1}/{len(time_slots)}: {time_slot}")
            
            success = rapid_submit_single(
                s, time_slot, roomid, seatid[0], captcha_pool, action
            )
            
            if success:
                logging.info(f"✅ 时间段 {time_slot} 预约成功！")
            else:
                logging.info(f"🔄 时间段 {time_slot} 预约失败，快速重试...")
                time.sleep(0.1)
                rapid_submit_single(s, time_slot, roomid, seatid[0], captcha_pool, action)
            
            if i < len(time_slots) - 1:
                time.sleep(0.2)
        
        captcha_pool.stop()


def main(users, action=False):
    logging.info(f"程序启动，立即登录 (action={'on' if action else 'off'})")
    
    usernames, passwords = None, None
    if action:
        usernames, passwords = get_user_credentials(action)
    
    logged_sessions, captcha_pools = pre_login_users(users, usernames, passwords, action)
    
    # 先等验证码池时间
    wait_until_preload_time(CAPTCHA_PRELOAD_AT, action)
    for pool in captcha_pools:
        if pool:
            pool.start_preloading()
            time.sleep(0.5)
    
    # 再等预约时间
    wait_for_target_time(RESERVE_TARGET_TIME, action)
    
    start_reservation_optimized(users, logged_sessions, captcha_pools, action)


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
    func_dict = {"reserve": main}
    with open(args.user, "r+") as data:
        usersdata = json.load(data)["reserve"]
    func_dict[args.method](usersdata, args.action)
