from utils import AES_Encrypt, enc, generate_captcha_key, verify_param
import json
import requests
import re
import time
import logging
import datetime
from urllib3.exceptions import InsecureRequestWarning
import concurrent.futures
from threading import Lock
import asyncio
import aiohttp


def get_date(day_offset: int = 0):
    today = datetime.datetime.now().date()
    offset_day = today + datetime.timedelta(days=day_offset)
    tomorrow = offset_day.strftime("%Y-%m-%d")
    return tomorrow


class reserve:
    def __init__(
        self,
        sleep_time=0.01,  # 进一步减少到0.01秒
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
        
        # 激进的网络优化
        self.requests = requests.session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=50,     # 大幅增加连接池
            pool_maxsize=50,        # 大幅增加最大连接数
            max_retries=0           # 完全禁用重试，避免浪费时间
        )
        self.requests.mount('http://', adapter)
        self.requests.mount('https://', adapter)
        
        # 激进的超时设置
        self.requests.timeout = (1, 2)  # 连接1秒，读取2秒
        
        self.token_pattern = re.compile("token = '(.*?)'")
        self.headers = {
            "Referer": "https://office.chaoxing.com/",
            "Host": "captcha.chaoxing.com",
            "Cache-Control": "no-cache",
            "Sec-Ch-Ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Linux"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Connection": "keep-alive",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        self.login_headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Cache-Control": "no-cache",
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
        
        # 预编译正则表达式
        self.token_regex = re.compile(r'id="submit_enc"\s+value="(.*?)"')
        self.value_regex = re.compile(r'value="(.*?)"')

    def _get_page_token(self, url, require_value=False):
        try:
            start_time = time.time()
            response = self.requests.get(url=url, verify=False, timeout=self.requests.timeout)
            html = response.content.decode("utf-8")
            
            # 使用预编译的正则表达式
            matches = self.token_regex.findall(html)
            value_matches = None
            
            if require_value:
                value_matches = self.value_regex.findall(html)
                if not matches:
                    logging.error(f"Failed to get token from {url}")
                    return "", ""
                if not value_matches:
                    logging.error(f"Failed to get submit value from {url}")
                    return matches[0], ""
            
            elapsed = time.time() - start_time
            logging.info(f"Token获取耗时: {elapsed:.3f}秒")
            return matches[0] if matches else "", value_matches[0] if value_matches else ""
        except requests.exceptions.RequestException as e:
            logging.error(f"Network error in _get_page_token: {e}")
            return "", ""

    def get_login_status(self):
        self.requests.headers = self.login_headers
        try:
            start_time = time.time()
            self.requests.get(url=self.login_page, verify=False, timeout=self.requests.timeout)
            elapsed = time.time() - start_time
            logging.info(f"登录状态检查耗时: {elapsed:.3f}秒")
        except requests.exceptions.RequestException as e:
            logging.error(f"Network error in get_login_status: {e}")

    def login(self, username, password):
        username = AES_Encrypt(username)
        password = AES_Encrypt(password)
        parm = {
            "fid": -1,
            "uname": username,
            "password": password,
            "refer": "http%3A%2F%2Foffice.chaoxing.com%2Ffront%2Fthird%2Fapps%2Fseat%2Fcode%3Fid%3D4219%26seatNum%3D380",
            "t": True,
        }
        try:
            start_time = time.time()
            jsons = self.requests.post(url=self.login_url, params=parm, verify=False, timeout=self.requests.timeout)
            obj = jsons.json()
            elapsed = time.time() - start_time
            logging.info(f"登录请求耗时: {elapsed:.3f}秒")
            
            if obj["status"]:
                logging.info(f"User {username[:10]}... login successfully")
                return (True, "")
            else:
                logging.info(f"User {username[:10]}... login failed. Please check password and username!")
                return (False, obj["msg2"])
        except requests.exceptions.RequestException as e:
            logging.error(f"Network error in login: {e}")
            return (False, "Network error")
        except json.JSONDecodeError as e:
            logging.error(f"JSON decode error in login: {e}")
            return (False, "Response parse error")

    def roomid(self, encode):
        url = f"https://office.chaoxing.com/data/apps/seat/room/list?cpage=1&pageSize=100&firstLevelName=&secondLevelName=&thirdLevelName=&deptIdEnc={encode}"
        try:
            json_data = self.requests.get(url=url, timeout=self.requests.timeout).content.decode("utf-8")
            ori_data = json.loads(json_data)
            for i in ori_data["data"]["seatRoomList"]:
                info = f'{i["firstLevelName"]}-{i["secondLevelName"]}-{i["thirdLevelName"]} id为：{i["id"]}'
                print(info)
        except Exception as e:
            logging.error(f"Error in roomid: {e}")

    def resolve_captcha_fast(self):
        """优化后的验证码解决方案"""
        logging.info(f"开始解析验证码")
        try:
            start_time = time.time()
            captcha_token, bg, tp = self.get_slide_captcha_data()
            captcha_time = time.time() - start_time
            logging.info(f"验证码数据获取耗时: {captcha_time:.3f}秒, token: {captcha_token}")
            
            # 并行下载验证码图片
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                c_captcha_headers = {
                    "Referer": "https://office.chaoxing.com/",
                    "Host": "captcha-b.chaoxing.com",
                    "Connection": "keep-alive",
                    "User-Agent": self.headers["User-Agent"],
                }
                
                future_bg = executor.submit(self.requests.get, bg, headers=c_captcha_headers, timeout=self.requests.timeout)
                future_tp = executor.submit(self.requests.get, tp, headers=c_captcha_headers, timeout=self.requests.timeout)
                
                bgc = future_bg.result()
                tpc = future_tp.result()
            
            download_time = time.time() - start_time - captcha_time
            logging.info(f"图片下载耗时: {download_time:.3f}秒")
            
            x = self.x_distance_fast(bgc.content, tpc.content)
            calc_time = time.time() - start_time - captcha_time - download_time
            logging.info(f"距离计算耗时: {calc_time:.3f}秒, 距离: {x}")

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
                timeout=self.requests.timeout
            )
            
            text = response.text.replace("jQuery33109180509737430778_1716381333117(", "").replace(")", "")
            data = json.loads(text)
            
            total_time = time.time() - start_time
            logging.info(f"验证码总耗时: {total_time:.3f}秒")
            
            try:
                validate_val = json.loads(data["extraData"])["validate"]
                return validate_val
            except KeyError as e:
                logging.info("验证码解析失败")
                return ""
        except Exception as e:
            logging.error(f"验证码解析异常: {e}")
            return ""

    def resolve_captcha(self):
        """保持原接口兼容性"""
        return self.resolve_captcha_fast()

    def get_slide_captcha_data(self):
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
        response = self.requests.get(url=url, params=params, headers=self.headers, timeout=self.requests.timeout)
        content = response.text

        data = content.replace("jQuery33107685004390294206_1716461324846(", ")").replace(")", "")
        data = json.loads(data)
        captcha_token = data["token"]
        bg = data["imageVerificationVo"]["shadeImage"]
        tp = data["imageVerificationVo"]["cutoutImage"]
        return captcha_token, bg, tp

    def x_distance_fast(self, bg, tp):
        """优化后的距离计算"""
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

        try:
            # 直接处理图片数据，不再下载
            bg_img = cv2.imdecode(np.frombuffer(bg, np.uint8), cv2.IMREAD_COLOR)
            tp_img = cut_slide(tp)
            
            # 使用更快的边缘检测参数
            bg_edge = cv2.Canny(bg_img, 50, 150)  # 降低阈值，提高速度
            tp_edge = cv2.Canny(tp_img, 50, 150)
            
            bg_pic = cv2.cvtColor(bg_edge, cv2.COLOR_GRAY2RGB)
            tp_pic = cv2.cvtColor(tp_edge, cv2.COLOR_GRAY2RGB)
            
            # 使用更快的模板匹配方法
            res = cv2.matchTemplate(bg_pic, tp_pic, cv2.TM_CCOEFF_NORMED)
            _, _, _, max_loc = cv2.minMaxLoc(res)
            return max_loc[0]
        except Exception as e:
            logging.error(f"Error in x_distance_fast: {e}")
            return 0

    def x_distance(self, bg, tp):
        """保持原接口兼容性"""
        c_captcha_headers = {
            "Referer": "https://office.chaoxing.com/",
            "Host": "captcha-b.chaoxing.com",
            "Connection": "keep-alive",
            "User-Agent": self.headers["User-Agent"],
        }
        try:
            bgc, tpc = self.requests.get(bg, headers=c_captcha_headers, timeout=self.requests.timeout), \
                      self.requests.get(tp, headers=c_captcha_headers, timeout=self.requests.timeout)
            return self.x_distance_fast(bgc.content, tpc.content)
        except Exception as e:
            logging.error(f"Error in x_distance: {e}")
            return 0

    def submit_single_seat_ultra_fast(self, times, roomid, seat, action):
        """极速单座位提交"""
        try:
            # 并行获取token和验证码
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                token_future = executor.submit(self._get_page_token, self.url.format(roomid, seat), True)
                
                if self.enable_slider:
                    captcha_future = executor.submit(self.resolve_captcha_fast)
                    token, value = token_future.result()
                    captcha = captcha_future.result()
                else:
                    token, value = token_future.result()
                    captcha = ""
            
            logging.info(f"座位 {seat} token: {token[:20]}...")
            
            return self.get_submit(
                self.submit_url,
                times=times,
                token=token,
                roomid=roomid,
                seatid=seat,
                captcha=captcha,
                action=action,
                value=value,
            )
        except Exception as e:
            logging.error(f"座位 {seat} 提交异常: {e}")
            return False

    def submit(self, times, roomid, seatid, action):
        """极速优化版提交"""
        start_time = time.time()
        
        if len(seatid) == 1:
            # 单个座位快速处理
            seat = seatid[0]
            for attempt in range(self.max_attempt):
                try:
                    suc = self.submit_single_seat_ultra_fast(times, roomid, seat, action)
                    if suc:
                        elapsed = time.time() - start_time
                        logging.info(f"🎉 座位预约成功！总耗时: {elapsed:.3f}秒")
                        return suc
                except Exception as e:
                    logging.error(f"尝试 {attempt+1} 失败: {e}")
                
                if attempt < self.max_attempt - 1:
                    time.sleep(self.sleep_time)
        else:
            # 多座位超高速并发
            for attempt in range(self.max_attempt):
                with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(seatid), 8)) as executor:
                    futures = {
                        executor.submit(self.submit_single_seat_ultra_fast, times, roomid, seat, action): seat 
                        for seat in seatid
                    }
                    
                    try:
                        for future in concurrent.futures.as_completed(futures, timeout=5):
                            try:
                                suc = future.result()
                                if suc:
                                    elapsed = time.time() - start_time
                                    seat = futures[future]
                                    logging.info(f"🎉 座位 {seat} 预约成功！总耗时: {elapsed:.3f}秒")
                                    
                                    # 立即取消其他任务
                                    for f in futures:
                                        if f != future:
                                            f.cancel()
                                    return suc
                            except Exception as e:
                                logging.error(f"并发任务执行失败: {e}")
                    except concurrent.futures.TimeoutError:
                        logging.warning(f"尝试 {attempt+1} 超时")
                
                if attempt < self.max_attempt - 1:
                    time.sleep(self.sleep_time)
        
        elapsed = time.time() - start_time
        logging.info(f"❌ 预约失败，总耗时: {elapsed:.3f}秒")
        return False

    def get_submit(self, url, times, token, roomid, seatid, captcha="", action=False, value=""):
        delta_day = 1 if self.reserve_next_day else 0
        day = datetime.date.today() + datetime.timedelta(days=0 + delta_day)
        if action:
            day = datetime.date.today() + datetime.timedelta(days=1 + delta_day)
            
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
        
        parm["enc"] = verify_param(parm, value)
        
        try:
            start_time = time.time()
            html = self.requests.post(
                url=url, params=parm, verify=True, timeout=self.requests.timeout
            ).content.decode("utf-8")
            
            elapsed = time.time() - start_time
            logging.info(f"提交请求耗时: {elapsed:.3f}秒")
            
            result = json.loads(html)
            self.submit_msg.append(f"{times[0]}~{times[1]}: {result}")
            logging.info(f"提交结果: {result}")
            return result["success"]
            
        except requests.exceptions.RequestException as e:
            logging.error(f"Network error in get_submit: {e}")
            return False
        except json.JSONDecodeError as e:
            logging.error(f"JSON decode error in get_submit: {e}")
            return False
        except Exception as e:
            logging.error(f"Unexpected error in get_submit: {e}")
            return False
