import utils
import alpha_diff_monitor
import time

def refreshtoken():
    import requests

    cookies = {
        '_ga': 'GA1.1.421334895.1727329259',
        'GMGN_LOCALE': 'zh-CN',
        'GMGN_THEME': 'dark',
        'GMGN_CHAIN': 'sol',
        '_ga_0XM0LYXGC8': 'deleted',
        'cf_clearance': 'gACJRZmlGc_8UJsIdeVyVV6tkogwuQ5AxkhX0m40Myc-1761628192-1.2.1.1-uiUKVGEPhDhOC9zb46Ccpz6KWI0b8Jx5fPpn1WC8yEYpNlIceMCi.HPur5qiU2VzuG1DaHGW5SHEzY1MbOjZuKaasz2go7s3ZzXPin9kvKIcLro2eJkzuhz.hp4djsKM8Vv803Ipf2gHitaGE.D1qA1uAL12RlDDKI9IpVQIbepwJF7MWw9.Iknn8hInj8tRx_8HDsOmgYnmYecgysxEr0w320vC1OkCBjAp7EyCiXk',
        'sid': 'gmgn%7C7e674587cb1640176169e211217dfab6',
        '_ga_UGLVBMV4Z0': 'GS1.2.1768495378402796.8193cb66cf2330ab7378027e46aececc.e%2FngVKOn51yfo4K2INr8Lg%3D%3D.yZFGzlPV9ZlZGcP2pXLEzA%3D%3D.m8Noo0%2FaOhWOdGkP01QrZA%3D%3D.uOeJTTK2IV5KWn5o83ETaw%3D%3D',
        '__cf_bm': 'k4sItLs4vlPpF2C8c96_56RYO6crrgK9aBqzUv6PEGc-1768496503-1.0.1.1-_e7v2JpB6STlsuD8AJY6XObFmOsOnojFm68H51L0s_QSKb.c88zghSNVEpz.bS8OQhQiK78zM4PsRniR.gjU9EXH4pGRI1juNcRtWIsK3WU',
        '_ga_0XM0LYXGC8': 'GS2.1.s1768493470$o2408$g1$t1768497146$j60$l0$h0',
    }

    headers = {
        'accept': 'application/json, text/plain, */*',
        'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'baggage': 'sentry-environment=production,sentry-release=20260115-9909-b6161f8,sentry-public_key=93c25bab7246077dc3eb85b59d6e7d40,sentry-trace_id=0caf413e481042d6978c31ff1cd40371,sentry-org_id=4505147559706624,sentry-transaction=%2Fportfolio%2F%5Bcode%5D,sentry-sampled=false,sentry-sample_rand=0.9595062563182333,sentry-sample_rate=0.01',
        'cache-control': 'no-cache',
        'content-type': 'application/json',
        'origin': 'https://gmgn.ai',
        'pragma': 'no-cache',
        'priority': 'u=1, i',
        'referer': 'https://gmgn.ai/portfolio/SisumwM3?chain=bsc&tab=holding',
        'sec-ch-ua': '"Google Chrome";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
        'sec-ch-ua-arch': '"x86"',
        'sec-ch-ua-bitness': '"64"',
        'sec-ch-ua-full-version': '"143.0.7499.193"',
        'sec-ch-ua-full-version-list': '"Google Chrome";v="143.0.7499.193", "Chromium";v="143.0.7499.193", "Not A(Brand";v="24.0.0.0"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-model': '""',
        'sec-ch-ua-platform': '"Windows"',
        'sec-ch-ua-platform-version': '"10.0.0"',
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'cors',
        'sec-fetch-site': 'same-origin',
        'sentry-trace': 'db84f83892e448eba7860b7a5e921fd6-9ffcd988675fe5e8-0',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36',
        # 'cookie': '_ga=GA1.1.421334895.1727329259; GMGN_LOCALE=zh-CN; GMGN_THEME=dark; GMGN_CHAIN=sol; _ga_0XM0LYXGC8=deleted; cf_clearance=gACJRZmlGc_8UJsIdeVyVV6tkogwuQ5AxkhX0m40Myc-1761628192-1.2.1.1-uiUKVGEPhDhOC9zb46Ccpz6KWI0b8Jx5fPpn1WC8yEYpNlIceMCi.HPur5qiU2VzuG1DaHGW5SHEzY1MbOjZuKaasz2go7s3ZzXPin9kvKIcLro2eJkzuhz.hp4djsKM8Vv803Ipf2gHitaGE.D1qA1uAL12RlDDKI9IpVQIbepwJF7MWw9.Iknn8hInj8tRx_8HDsOmgYnmYecgysxEr0w320vC1OkCBjAp7EyCiXk; sid=gmgn%7C7e674587cb1640176169e211217dfab6; _ga_UGLVBMV4Z0=GS1.2.1768495378402796.8193cb66cf2330ab7378027e46aececc.e%2FngVKOn51yfo4K2INr8Lg%3D%3D.yZFGzlPV9ZlZGcP2pXLEzA%3D%3D.m8Noo0%2FaOhWOdGkP01QrZA%3D%3D.uOeJTTK2IV5KWn5o83ETaw%3D%3D; __cf_bm=k4sItLs4vlPpF2C8c96_56RYO6crrgK9aBqzUv6PEGc-1768496503-1.0.1.1-_e7v2JpB6STlsuD8AJY6XObFmOsOnojFm68H51L0s_QSKb.c88zghSNVEpz.bS8OQhQiK78zM4PsRniR.gjU9EXH4pGRI1juNcRtWIsK3WU; _ga_0XM0LYXGC8=GS2.1.s1768493470$o2408$g1$t1768497146$j60$l0$h0',
    }

    params = {
    'device_id': 'f58d99b1-6a60-4fc8-b181-e01a6fca2427',
    'fp_did': '5c6d41de35d26eaad98548f2c66762c8',
    'client_id': 'gmgn_web_20260115-9909-b6161f8',
        'from_app': 'gmgn',
        'app_ver': '20260115-9909-b6161f8',
        'tz_name': 'Asia/Shanghai',
        'tz_offset': '28800',
        'app_lang': 'zh-CN',
        'os': 'web',
        'worker': '0',
    }

    json_data = {
        'refresh_token': 'eyJhbGciOiJFUzI1NiIsInR5cCI6IkpXVCJ9.eyJhdWQiOiJnbWduLmFpL3JlZnJlc2giLCJkYXRhIjp7InVzZXJfaWQiOiJlNTI3Y2EyYy03YmJjLTQ1NWMtODEzYi0xOGZkM2MzNTNkMWEiLCJjbGllbnRfaWQiOiJnbWduX3dlYl8yMDI2MDEwNi05NTc4LTAxOThkNTciLCJkZXZpY2VfaWQiOiIxNjUwYjYyZC00Y2FmLTQzNjYtYmVhZS1lYzY0YjJlODk2MTEiLCJmYXRoZXJfaWQiOiIiLCJmaW5nZXJwcmludCI6InYxNjQxNTA2MDI4YWMxZTFjNGQwZDQwOTNjMjkxYTRhOTYiLCJhcHAiOiJnbWduIiwicGxhdGZvcm0iOiJ3ZWIifSwiZXhwIjoxNzcwMzE1ODcxLCJpYXQiOjE3Njc3MjM4NzEsImlzcyI6ImdtZ24uYWkvc2lnbmVyIiwianRpIjoiY2NkNDAxNTMtZGNhMy00ODNhLWEyZWMtNTZmOThlODU0ODA2IiwibmJmIjoxNzY3NzIzODcxLCJzdWIiOiJnbWduLmFpL3JlZnJlc2giLCJ1c2VyX2lkIjoiZTUyN2NhMmMtN2JiYy00NTVjLTgxM2ItMThmZDNjMzUzZDFhIiwidmVyIjoiMS4wIn0.uKjQeM3r4Veen1GVV5_oBRb-UwOSlWdzpPlgnbCa000KxuCWGeLbzAKXEDs6JHBVYiNPX30f90FmWNWM1T_Cyg',
        'refresh_token': "eyJhbGciOiJFUzI1NiIsInR5cCI6IkpXVCJ9.eyJhdWQiOiJnbWduLmFpL3JlZnJlc2giLCJkYXRhIjp7InVzZXJfaWQiOiI2N2IzN2MyMi00NjA2LTQ2ZmMtODUzZi0zNWMyMDhmNDRhMzYiLCJjbGllbnRfaWQiOiJnbWduX3dlYl8yMDI2MDExNS05OTA5LWI2MTYxZjgiLCJkZXZpY2VfaWQiOiJmNThkOTliMS02YTYwLTRmYzgtYjE4MS1lMDFhNmZjYTI0MjciLCJmYXRoZXJfaWQiOiIiLCJmaW5nZXJwcmludCI6InYxNjE0ZjljZTRkZTYwMTdmNTZjYjFjYjhkZDNiZjJkNTAiLCJhcHAiOiJnbWduIiwicGxhdGZvcm0iOiJ3ZWIifSwiZXhwIjoxNzcxMDk2ODMxLCJpYXQiOjE3Njg1MDQ4MzEsImlzcyI6ImdtZ24uYWkvc2lnbmVyIiwianRpIjoiYjRlN2YxYWItODMwOC00ZjlkLWIyMjAtMzBjNzg0Y2E2ZjkxIiwibmJmIjoxNzY4NTA0ODMxLCJzdWIiOiJnbWduLmFpL3JlZnJlc2giLCJ1c2VyX2lkIjoiNjdiMzdjMjItNDYwNi00NmZjLTg1M2YtMzVjMjA4ZjQ0YTM2IiwidmVyIjoiMS4wIn0.hvuK2tpr39Mqgu_LSU-tddS1Ph6pP1QHkxxDy8yFDMR4wga6thMqZInJhmTVW5Hy3vZ_R_BMu2Nqvb1Su-tAWA",
    }

    response = requests.post(
        'https://gmgn.ai/account/account/refresh_access_token',
        params=params,
        cookies=cookies,
        headers=headers,
        json=json_data,
    )
    print(response.text)
    return response

    # Note: json_data will not be serialized by requests
    # exactly as it was in the original request.
    #data = '{"refresh_token":"eyJhbGciOiJFUzI1NiIsInR5cCI6IkpXVCJ9.eyJhdWQiOiJnbWduLmFpL3JlZnJlc2giLCJkYXRhIjp7InVzZXJfaWQiOiJlNTI3Y2EyYy03YmJjLTQ1NWMtODEzYi0xOGZkM2MzNTNkMWEiLCJjbGllbnRfaWQiOiJnbWduX3dlYl8yMDI2MDEwNi05NTc4LTAxOThkNTciLCJkZXZpY2VfaWQiOiIxNjUwYjYyZC00Y2FmLTQzNjYtYmVhZS1lYzY0YjJlODk2MTEiLCJmYXRoZXJfaWQiOiIiLCJmaW5nZXJwcmludCI6InYxNjQxNTA2MDI4YWMxZTFjNGQwZDQwOTNjMjkxYTRhOTYiLCJhcHAiOiJnbWduIiwicGxhdGZvcm0iOiJ3ZWIifSwiZXhwIjoxNzcwMzE1ODcxLCJpYXQiOjE3Njc3MjM4NzEsImlzcyI6ImdtZ24uYWkvc2lnbmVyIiwianRpIjoiY2NkNDAxNTMtZGNhMy00ODNhLWEyZWMtNTZmOThlODU0ODA2IiwibmJmIjoxNzY3NzIzODcxLCJzdWIiOiJnbWduLmFpL3JlZnJlc2giLCJ1c2VyX2lkIjoiZTUyN2NhMmMtN2JiYy00NTVjLTgxM2ItMThmZDNjMzUzZDFhIiwidmVyIjoiMS4wIn0.uKjQeM3r4Veen1GVV5_oBRb-UwOSlWdzpPlgnbCa000KxuCWGeLbzAKXEDs6JHBVYiNPX30f90FmWNWM1T_Cyg"}'
    #response = requests.post(
    #    'https://gmgn.ai/account/account/refresh_access_token',
    #    params=params,
    #    cookies=cookies,
    #    headers=headers,
    #    data=data,
    #)
    #     

last_check_time = 0
def checktoken():
    global last_check_time
    elapsed = time.time() - last_check_time
    if elapsed < 60*10:
        return
    last_check_time = time.time()
    '''
{
  "code": 0,
  "message": "success",
  "data": {
    "data": {
      "expire_at": 1768500046,
      "token": "eyJhbGciOiJFUzI1NiIsInR5cCI6IkpXVCJ9.eyJhdWQiOiJnbWduLmFpL2FjY2VzcyIsImRhdGEiOnsidXNlcl9pZCI6ImU1MjdjYTJjLTdiYmMtNDU1Yy04MTNiLTE4ZmQzYzM1M2QxYSIsImNsaWVudF9pZCI6ImdtZ25fd2ViXzIwMjYwMTA2LTk1NzgtMDE5OGQ1NyIsImRldmljZV9pZCI6IjE2NTBiNjJkLTRjYWYtNDM2Ni1iZWFlLWVjNjRiMmU4OTYxMSIsImZhdGhlcl9pZCI6ImNjZDQwMTUzLWRjYTMtNDgzYS1hMmVjLTU2Zjk4ZTg1NDgwNiIsImZpbmdlcnByaW50IjoidjE2NDE1MDYwMjhhYzFlMWM0ZDBkNDA5M2MyOTFhNGE5NiIsImFwcCI6ImdtZ24iLCJwbGF0Zm9ybSI6IndlYiJ9LCJleHAiOjE3Njg1MDAwNDYsImlhdCI6MTc2ODQ5ODI0NiwiaXNzIjoiZ21nbi5haS9zaWduZXIiLCJqdGkiOiJhNTY0ZDE4Yy01ZDJhLTRhMzAtOGYxMi02MWU2MDhkNzFiYTciLCJuYmYiOjE3Njg0OTgyNDYsInN1YiI6ImdtZ24uYWkvYWNjZXNzIiwidXNlcl9pZCI6ImU1MjdjYTJjLTdiYmMtNDU1Yy04MTNiLTE4ZmQzYzM1M2QxYSIsInZlciI6IjEuMCJ9.Ob-09ddxHZeHTWeN8Tu87GpQJCDs5CBpRplbi3jxLwnYDEMYsO4B0iFktXdxtgdSSW3EXQ1SiflFaQT54f-k8Q"
    },
    "done": true,
    "step": 1
  }
}
'''
    try:
        response = refreshtoken()
        refresh_token = response.json()['data']['data']['token']
        with open('gmgn_authorization.txt', 'w') as f:
            f.write(refresh_token)
    except Exception as e:
        print(e)
        utils.send_notification_feishu(utils.feishu_myself,f'checktoken test gmgn error:{e}', 'test_gmgn_cookie_ok')
while 1:    
    try:
    
        checktoken()
        with open('gmgn_authorization.txt', 'r') as f:
            gmgn_Bearer = f.read().strip()
        alpha_diff_monitor.GMGN_HEADERS['Authorization'] = f'Bearer {gmgn_Bearer}'
        checkgmgn = utils.test_gmgn_cookie_ok(alpha_diff_monitor.GMGN_HEADERS, alpha_diff_monitor.GMGN_COOKIES)
        print(f'status code {checkgmgn.status_code}')
        if checkgmgn.status_code != 200:
            checkgmgn = utils.test_gmgn_cookie_ok(alpha_diff_monitor.GMGN_HEADERS, alpha_diff_monitor.GMGN_COOKIES)
            if checkgmgn.status_code != 200:
                print('cookie is not ok')
                utils.send_notification_feishu(utils.feishu_myself,f'test gmgn error:{checkgmgn.text[:100]}', 'test_gmgn_cookie_ok')
                break
        time.sleep(10)
    except Exception as e:
        utils.send_notification_feishu(utils.feishu_myself,f'test gmgn Exception  error:{e}', 'test_gmgn_cookie_ok')
        print(e)

