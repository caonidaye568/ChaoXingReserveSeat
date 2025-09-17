from utils import AES_Encrypt, enc, generate_captcha_key, verify_param
import json
import requests
import re
import time
import logging
import datetime
import threading
from urllib3.exceptions import InsecureRequestWarning
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

def get_date(day_offset: int = 0):
    today = datetime.datetime.now().date()
    offset_day = today + datetime.timedelta(days=day_offset)
    tomorrow = offset_day.strftime("%Y-%m-%d")
    return tomorrow

class reserve:
    def __init__(
        self,
        sleep_time=0.05,  # 进一步减少默认延迟
        max_attempt=50,
        enable_slider=False,
        reserve_next_day=False,
    ):
        self.login_page = (
            "https://passport2.chaoxing.com/mlogin?loginType=1&newversion=true&fid="
        )
        self.url = (
            "https://office.chaoxing.com/front/third/apps/seat/code?id={}&seatNum={}"
        )
        self.submit_url = "https://office.chaoxing.com/data/apps/seat/submit"
        self.seat_url = "https://office.chaoxing.com/data/apps/seat/getusedtimes"
        self.login_url = "https://passport2.chaoxing.com/fanyalogin"
        self.token = ""
        self.success_times = 0
        self.fail_dict = []
        self.submit_msg = []
        
        # 优化requests session配置
        self.requests = requests.session()
        self._setup_session()
        
        self.token_pattern = re.compile("token = '(.*?)'")
        self.headers = {
            "Referer": "https://office.chaoxing.com/",
            "Host": "captcha.chaoxing.com",
            "Pragma": "no-cache",
            "Sec-Ch-Ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Linux"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        }
        self.login_headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "accept-encoding": "gzip, deflate, br, zstd",
            "cache-control": "no-cache",
            "Connection": "keep-alive",
            "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 10_3_1 like Mac OS X) AppleWebKit/603.1.3 (KHTML, like Gecko) Version/10.0 Mobile/14E304 Safari/602.1 wechatdevtools/1.05.2109131 MicroMessenger/8.0.5 Language/zh_CN webview/16364215743155638",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Host": "passport2.chaoxing.com",
        }

        self.sleep_time = sleep_time
        self.max_attempt = max_attempt
        self.enable_slider = enable_slider
        self.reserve_next_day = reserve_next_day
        requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

    def _setup_session(self):
        """优化session配置"""
        # 设置重试策略
        retry_strategy = Retry(
            total=2,  # 减少重试次数
            backoff_factor=0.1,  # 减少重试间隔
            status_forcelist=[429, 500, 502, 503, 504],
        )
        
        # 设置适配器
        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=20,  # 增加连接池大小
            pool_maxsize=50,  # 增加最大连接数
        )
        
        self.requests.mount("http://", adapter)
        self.requests.mount("https://", adapter)
        
        # 设置超时
        self.requests.request = self._add_timeout(self.requests.request)
        
        # 启用keep-alive
        self.requests.keep_alive = True

    def _add_timeout(self, func):
        """为请求添加超时"""
        def wrapper(*args, **kwargs):
            kwargs.setdefault('timeout', (3, 10))  # 连接超时3s，读取超时10s
            return func(*args, **kwargs)
        return wrapper

    # login and page token - 优化版本
    def _get_page_token(self, url, require_value=False):
        """优化的token获取方法"""
        try:
            start_time = time.time()
            response = self.requests.get(url=url, verify=False, timeout=(2, 5))  # 缩短超时
            html = response.content.decode("utf-8")
            
            # 优化正则匹配，按优先级顺序尝试
            matches = []
            patterns = [
                r'id="submit_enc"\s+value="([^"]*)"',  # 最常见的模式
                r'name="token"\s+value="([^"]*)"',     # 备选模式1  
                r"token\s*=\s*'([^']*)'",              # 备选模式2
            ]
            
            for i, pattern in enumerate(patterns):
                matches = re.findall(pattern, html)
                if matches:
                    logging.debug(f"Token匹配成功，使用模式{i+1}")
                    break
            
            value_matches = None
            if require_value:
                value_patterns = [
                    r'id="submit_enc"\s+value="([^"]*)"',  # 首先尝试submit_enc
                    r'value="([^"]*)"'  # 通用value匹配
                ]
                
                for pattern in value_patterns:
                    value_matches = re.findall(pattern, html)
                    if value_matches:
                        break
                        
                if not matches:
                    logging.error(f"Failed to get token from {url}")
                    return "", ""
                if not value_matches:
                    logging.warning(f"Failed to get submit value from {url}, using token as value")
                    value_matches = matches  # 使用token作为value的备选方案
            
            end_time = time.time()
            token_str = matches[0] if matches else ""
            value_str = value_matches[0] if value_matches else ""
            
            logging.debug(f"Token获取耗时: {end_time - start_time:.3f}s, token长度: {len(token_str)}, value长度: {len(value_str)}")
            
            return token_str, value_str
            
        except Exception as e:
            logging.error(f"获取token时发生异常: {e}")
            return "", ""

    def get_login_status(self):
        self.requests.headers = self.login_headers
        self.requests.get(url=self.login_page, verify=False)

    def login(self, username, password):
        username_enc = AES_Encrypt(username)
        password_enc = AES_Encrypt(password)
        parm = {
            "fid": -1,
            "uname": username_enc,
            "password": password_enc,
            "refer": "http%3A%2F%2Foffice.chaoxing.com%2Ffront%2Fthird%2Fapps%2Fseat%2Fcode%3Fid%3D4219%26seatNum%3D380",
            "t": True,
        }
        jsons = self.requests.post(url=self.login_url, params=parm, verify=False)
        obj = jsons.json()
        if obj["status"]:
            logging.info(f"User {username_enc} login successfully")
            return (True, "")
        else:
            logging.info(
                f"User {username} login failed. Please check you password and username! "
            )
            return (False, obj["msg2"])

    # extra: get roomid
    def roomid(self, encode):
        url = f"https://office.chaoxing.com/data/apps/seat/room/list?cpage=1&pageSize=100&firstLevelName=&secondLevelName=&thirdLevelName=&deptIdEnc={encode}"
        json_data = self.requests.get(url=url).content.decode("utf-8")
        ori_data = json.loads(json_data)
        for i in ori_data["data"]["seatRoomList"]:
            info = f'{i["firstLevelName"]}-{i["secondLevelName"]}-{i["thirdLevelName"]} id为：{i["id"]}'
            print(info)

    # solve captcha - 优化版本
    def resolve_captcha(self):
        """优化的验证码解决方法"""
        start_time = time.time()
        logging.info(f"Start to resolve captcha token")
        
        try:
            captcha_token, bg, tp = self.get_slide_captcha_data()
            logging.info(f"Successfully get prepared captcha_token {captcha_token}")
            logging.info(f"Captcha Image URL-small {tp}, URL-big {bg}")
            
            x = self.x_distance(bg, tp)
            logging.info(f"Successfully calculate the captcha distance {x}")

            params = {
                "callback": "jQuery33109180509737430778_1716381333117",
                "captchaId": "42sxgHoTPTKbt0uZxPJ7ssOvtXr3ZgZ1",
                "type": "slide",
                "token": captcha_token,
                "textClickArr": json.dumps([{"x": x}]),
                "coordinate": json.dumps([]),
                "runEnv": "10",
                "version": "1.1.18",
                "_": int(time.time() * 1000),
            }
            response = self.requests.get(
                f"https://captcha.chaoxing.com/captcha/check/verification/result",
                params=params,
                headers=self.headers,
            )
            text = response.text.replace(
                "jQuery33109180509737430778_1716381333117(", ""
            ).replace(")", "")
            data = json.loads(text)
            logging.info(f"Successfully resolve the captcha token {data}")
            
            try:
                validate_val = json.loads(data["extraData"])["validate"]
                end_time = time.time()
                logging.debug(f"验证码解决总耗时: {end_time - start_time:.3f}s")
                return validate_val
            except KeyError as e:
                logging.warning("Can't load validate value. Maybe server return mistake.")
                return ""
                
        except Exception as e:
            logging.error(f"验证码解决过程中发生异常: {e}")
            return ""

    def get_slide_captcha_data(self):
        """获取滑块验证码数据"""
        url = "https://captcha.chaoxing.com/captcha/get/verification/image"
        timestamp = int(time.time() * 1000)
        capture_key, token = generate_captcha_key(timestamp)
        referer = f"https://office.chaoxing.com/front/third/apps/seat/code?id=3993&seatNum=0199"
        params = {
            "callback": f"jQuery33107685004390294206_1716461324846",
            "captchaId": "42sxgHoTPTKbt0uZxPJ7ssOvtXr3ZgZ1",
            "type": "slide",
            "version": "1.1.18",
            "captchaKey": capture_key,
            "token": token,
            "referer": referer,
            "_": timestamp,
            "d": "a",
            "b": "a",
        }
        response = self.requests.get(url=url, params=params, headers=self.headers)
        content = response.text

        data = content.replace(
            "jQuery33107685004390294206_1716461324846(", ")"
        ).replace(")", "")
        data = json.loads(data)
        captcha_token = data["token"]
        bg = data["imageVerificationVo"]["shadeImage"]
        tp = data["imageVerificationVo"]["cutoutImage"]
        return captcha_token, bg, tp

    def x_distance(self, bg, tp):
        """计算滑块距离 - 串行优化版本"""
        import numpy as np
        import cv2

        def cut_slide(slide):
            slider_array = np.frombuffer(slide, np.uint8)
            slider_image = cv2.imdecode(slider_array, cv2.IMREAD_UNCHANGED)
            slider_part = slider_image[:, :, :3]
            mask = slider_image[:, :, 3]
            mask[mask != 0] = 255
            x, y, w, h = cv2.boundingRect(mask)
            cropped_image = slider_part[y : y + h, x : x + w]
            return cropped_image

        c_captcha_headers = {
            "Referer": "https://office.chaoxing.com/",
            "Host": "captcha-b.chaoxing.com",
            "Pragma": "no-cache",
            "Sec-Ch-Ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Linux"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        }
        
        try:
            # 串行下载验证码图片（避免被检测）
            start_time = time.time()
            
            bg_content = self.requests.get(bg, headers=c_captcha_headers, timeout=3).content
            tp_content = self.requests.get(tp, headers=c_captcha_headers, timeout=3).content
            
            download_time = time.time() - start_time
            logging.debug(f"验证码图片下载耗时: {download_time:.3f}s")
            
            bg_img = cv2.imdecode(np.frombuffer(bg_content, np.uint8), cv2.IMREAD_COLOR)
            tp_img = cut_slide(tp_content)
            
            bg_edge = cv2.Canny(bg_img, 100, 200)
            tp_edge = cv2.Canny(tp_img, 100, 200)
            bg_pic = cv2.cvtColor(bg_edge, cv2.COLOR_GRAY2RGB)
            tp_pic = cv2.cvtColor(tp_edge, cv2.COLOR_GRAY2RGB)
            res = cv2.matchTemplate(bg_pic, tp_pic, cv2.TM_CCOEFF_NORMED)
            _, _, _, max_loc = cv2.minMaxLoc(res)
            tl = max_loc
            
            process_time = time.time() - start_time - download_time
            logging.debug(f"验证码图片处理耗时: {process_time:.3f}s")
            
            return tl[0]
            
        except Exception as e:
            logging.error(f"验证码图片处理失败: {e}")
            return 120  # 返回默认值

    def submit(self, times, roomid, seatid, action):
        """优化后的提交方法 - 用于兼容旧版本调用"""
        captcha = self.resolve_captcha() if self.enable_slider else ""
        
        for seat in seatid:
            attempt_count = 0
            while attempt_count < self.max_attempt:
                token, value = self._get_page_token(
                    self.url.format(roomid, seat), require_value=True
                )
                logging.info(f"Get token: {token}")
                
                success = self.get_submit(
                    self.submit_url,
                    times=times,
                    token=token,
                    roomid=roomid,
                    seatid=seat,
                    captcha=captcha,
                    action=action,
                    value=value,
                )
                if success:
                    return True
                    
                attempt_count += 1
                if attempt_count < self.max_attempt:
                    time.sleep(self.sleep_time)
                    
        return False

    def get_submit(
        self, url, times, token, roomid, seatid, captcha="", action=False, value=""
    ):
        """优化的提交方法"""
        start_time = time.time()
        
        try:
            delta_day = 1 if self.reserve_next_day else 0
            
            if action:
                # GitHub Action 环境是 UTC 时间，需要转换为北京时间
                beijing_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
                day = beijing_now.date() + datetime.timedelta(days=delta_day)
            else:
                # 本地环境直接使用当地时间
                day = datetime.date.today() + datetime.timedelta(days=delta_day)
                
            parm = {
                "roomId": roomid,
                "startTime": times[0],
                "endTime": times[1],
                "day": str(day),
                "seatNum": seatid,
                "captcha": captcha,
                "token": token,
                "type": "1",
                "verifyData": "1",
            }
            logging.info(f"submit parameter {parm} ")
            
            # 优化参数编码
            parm["enc"] = verify_param(parm, value)
            
            # 使用POST提交，优化超时设置
            response = self.requests.post(
                url=url, 
                params=parm, 
                verify=True,
                timeout=(2, 8)  # 连接超时2s，读取超时8s
            )
            
            html = response.content.decode("utf-8")
            result = json.loads(html)
            
            self.submit_msg.append(
                times[0] + "~" + times[1] + ":  " + str(result)
            )
            
            end_time = time.time()
            logging.info(f"提交耗时: {end_time - start_time:.3f}s - {result}")
            
            return result["success"]
            
        except Exception as e:
            end_time = time.time()
            logging.error(f"提交过程中发生异常: {e}，耗时: {end_time - start_time:.3f}s")
            return False
