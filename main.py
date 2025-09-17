import json
import time
import argparse
import os
import logging
import datetime
import threading
import copy  # 导入copy模块，用于深层复制
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

SLEEPTIME = 0.1
RESERVE_TARGET_TIME = "22:00:00"
ENABLE_SLIDER = True
MAX_ATTEMPT = 1
RESERVE_NEXT_DAY = True
CAPTCHA_POOL_SIZE = 5
CAPTCHA_PRELOAD_TIME = 5

class CaptchaPool:
    """验证码缓存池 (为登录和预加载阶段服务)"""
    def __init__(self, session, pool_size=CAPTCHA_POOL_SIZE):
        self.session = session
        self.pool_size = pool_size
        self.captcha_queue = Queue()
        self.is_active = True
        self.lock = threading.Lock()
        
    def start_preloading(self):
        logging.info(f"🔄 开始预加载验证码池，目标数量: {self.pool_size}")
        
        def preload_worker():
            while self.is_active and self.captcha_queue.qsize() < self.pool_size:
                try:
                    captcha = self.session.resolve_captcha()
                    if captcha:
                        self.captcha_queue.put(captcha)
                        logging.info(f"✅ 验证码预加载成功，当前池大小: {self.captcha_queue.qsize()}")
                    time.sleep(0.5)
                except Exception as e:
                    logging.warning(f"⚠️ 验证码预加载失败: {e}")
                    time.sleep(1)
        
        thread = threading.Thread(target=preload_worker, daemon=True)
        thread.start()
    
    def get_captcha(self):
        if not self.captcha_queue.empty():
            return self.captcha_queue.get()
        else:
            logging.warning("⚠️ 验证码池为空，临时生成验证码")
            return self.session.resolve_captcha()
    
    def stop(self):
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

# ####################################################################
# ################ START: 并行预约逻辑核心修改 #######################
# ####################################################################

def parallel_submit_worker(base_session, times, roomid, seatid, action):
    """
    为单个预约任务设计的独立工作函数.
    每个线程会获得一个独立的session副本.
    """
    start_time = time.time()
    
    # 关键改动：为当前线程创建一个独立的session深层副本
    session_copy = copy.deepcopy(base_session)
    
    try:
        # 1. 使用独立的session副本即时获取验证码
        captcha = session_copy.resolve_captcha()
        
        # 2. 使用独立的session副本即时获取token
        token, value = session_copy._get_page_token(
            session_copy.url.format(roomid, seatid), require_value=True
        )
        logging.info(f"⚡ 线程 {times[0]}-{times[1]} 获取到独立Token: {token}")
        
        # 3. 使用独立的session副本提交请求
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
        if success:
            logging.info(f"🎉 (线程 {times[0]}-{times[1]}) 预约成功！耗时: {total_time:.2f}s")
        else:
            logging.warning(f"❌ (线程 {times[0]}-{times[1]}) 预约失败，耗时: {total_time:.2f}s")
            
        return success
        
    except Exception as e:
        total_time = time.time() - start_time
        logging.error(f"💥 (线程 {times[0]}-{times[1]}) 预约异常: {e}，耗时: {total_time:.2f}s")
        return False

def start_reservation_parallel(users, logged_sessions, action):
    """
    开始并行预约 (修改后的版本)
    """
    current_dayofweek = get_current_dayofweek(action)
    
    for index, user in enumerate(users):
        username, _, times, roomid, seatid, daysofweek = user.values()
        
        if current_dayofweek not in daysofweek:
            continue
            
        base_session = logged_sessions[index]
        if base_session is None:
            continue
        
        logging.info(f"🚀 开始并行预约 - 用户 {username}")
        
        time_slots = times if isinstance(times[0], list) else [times]
        
        # 使用线程池并发执行所有时间段的预约
        with ThreadPoolExecutor(max_workers=len(time_slots)) as executor:
            # 为每个任务提交一个worker，并传入主session以供复制
            futures = [
                executor.submit(parallel_submit_worker, base_session, slot, roomid, seatid[0], action)
                for slot in time_slots
            ]

            # 等待所有结果并汇总
            successful_slots = []
            for i, future in enumerate(futures):
                try:
                    if future.result():
                       successful_slots.append(time_slots[i])
                except Exception as e:
                    logging.error(f"💥 线程池任务 {time_slots[i]} 执行出错: {e}")

            if successful_slots:
                logging.info(f"🎊 用户 {username} 预约汇总：成功预约了 {len(successful_slots)} 个时间段: {successful_slots}")
            else:
                logging.warning(f"😞 用户 {username} 预约汇总：所有时间段预约均失败")


# ####################################################################
# ################# END: 并行预约逻辑核心修改 ########################
# ####################################################################

def pre_login_users(users, usernames, passwords, action):
    """提前登录所有用户并预热"""
    logged_sessions = []
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
            logged_sessions.append(s)
        else:
            logging.error(f"User {username} login failed: {msg}")
            logged_sessions.append(None)
    
    return logged_sessions

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
        
        # 提前 0.1 秒唤醒，应对可能的延迟
        if wait_seconds > 0.1:
            logging.info(f"距离目标时间 {target_time}（北京时间）还有 {wait_seconds:.1f} 秒，sleep……")
            time.sleep(wait_seconds - 0.1)
    
    while get_current_time(action) < target_time:
        time.sleep(0.001) # 毫秒级等待，确保精准

    logging.info(f"到达目标时间 {target_time}（北京时间），开始预约")

def main(users, action=False):
    logging.info(f"程序启动，立即登录 (action={'on' if action else 'off'})")
    
    usernames, passwords = None, None
    if action:
        usernames, passwords = get_user_credentials(action)
    
    logged_sessions = pre_login_users(users, usernames, passwords, action)
    
    wait_for_target_time(RESERVE_TARGET_TIME, action)
    
    # 调用新的并行预约函数
    start_reservation_parallel(users, logged_sessions, action)

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
        
        # 调试并行逻辑
        logging.info("--- 调试并行预约 ---")
        start_reservation_parallel([user], [s], action)
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
