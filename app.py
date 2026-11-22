from flask import Flask, request, jsonify
import asyncio
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from google.protobuf.json_format import MessageToJson
import binascii
import aiohttp
import requests
import json
import like_pb2
import like_count_pb2
import uid_generator_pb2
from google.protobuf.message import DecodeError
import os
import hmac
import keys_manager

try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Ek saath max kitni requests parallel jayengi (tokens ki total ginti isse alag hai).
LIKE_CONCURRENCY = max(1, int(os.environ.get("LIKE_CONCURRENCY", "100")))
# Ek request ka max time (seconds)
REQUEST_TIMEOUT = float(os.environ.get("LIKE_REQUEST_TIMEOUT", "20"))


def xpopu_token_file(server_name):
    if server_name == "IND":
        return os.path.join(BASE_DIR, "token_ind.json")
    elif server_name in {"BR", "US", "SAC", "NA"}:
        return os.path.join(BASE_DIR, "token_br.json")
    return os.path.join(BASE_DIR, "token_bd.json")


def xpopu_clean_tokens(raw):
    """
    token file ke raw data se sirf valid, unique tokens nikalta hai.
    Ek token = ek like, isliye duplicate / khali / placeholder tokens hata diye jate hain.
    Returns: [{"token": "..."}, ...]
    """
    if not isinstance(raw, list):
        return []
    cleaned, seen = [], set()
    for item in raw:
        token = item.get("token") if isinstance(item, dict) else item
        if not isinstance(token, str):
            continue
        token = token.strip()
        if not token or token.upper().startswith("YOUR_JWT_TOKEN"):
            continue
        if token in seen:
            continue
        seen.add(token)
        cleaned.append({"token": token})
    return cleaned


def xpopu_load_tokens(server_name):
    """
    Region ki token file padhta hai (har baar fresh read, to jitne token file mein
    update honge utne hi use honge). Valid token na mile to khali list [].
    """
    path = xpopu_token_file(server_name)
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            return []
        raw = json.loads(content)
    except Exception as e:
        app.logger.error(f"Error loading tokens for server {server_name} ({path}): {e}")
        return []
    tokens = xpopu_clean_tokens(raw)
    app.logger.info(f"{server_name}: {len(tokens)} valid unique tokens loaded")
    return tokens

def xpopu_encrypt_message(plaintext):
    try:
        key = b'Yg&tc%DEuh6%Zc^8'
        iv = b'6oyZDr22E3ychjM%'
        cipher = AES.new(key, AES.MODE_CBC, iv)
        padded_message = pad(plaintext, AES.block_size)
        encrypted_message = cipher.encrypt(padded_message)
        return binascii.hexlify(encrypted_message).decode('utf-8')
    except Exception as e:
        app.logger.error(f"Error encrypting message: {e}")
        return None

def xpopu_create_protobuf_message(user_id, region):
    try:
        message = like_pb2.like()
        message.uid = int(user_id)
        message.region = region
        return message.SerializeToString()
    except Exception as e:
        app.logger.error(f"Error creating protobuf message: {e}")
        return None

async def xpopu_send_request(session, encrypted_uid, token, url):
    """Ek token se ek like request. Returns (success: bool, status_code | None)."""
    try:
        edata = bytes.fromhex(encrypted_uid)
        headers = {
            'User-Agent': "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
            'Connection': "Keep-Alive",
            'Accept-Encoding': "gzip",
            'Authorization': f"Bearer {token}",
            'Content-Type': "application/x-www-form-urlencoded",
            'Expect': "100-continue",
            'X-Unity-Version': "2018.4.11f1",
            'X-GA': "v1 1",
            'ReleaseVersion': "OB55"
        }
        async with session.post(url, data=edata, headers=headers) as response:
            if response.status != 200:
                app.logger.error(f"Request failed with status code: {response.status}")
                return False, response.status
            await response.read()
            return True, 200
    except Exception as e:
        app.logger.error(f"Exception in send_request: {e}")
        return False, None


async def xpopu_send_multiple_requests(uid, server_name, url, tokens):
    """
    Token file mein jitne valid tokens hain, EXACTLY utni requests bhejta hai
    (1 token = 1 like request). Pehle yahan 2000 fixed tha.
    Returns dict: {"requests_sent": N, "success": S, "failed": F} ya error par None.
    """
    try:
        protobuf_message = xpopu_create_protobuf_message(uid, server_name)
        if protobuf_message is None:
            app.logger.error("Failed to create protobuf message.")
            return None
        encrypted_uid = xpopu_encrypt_message(protobuf_message)
        if encrypted_uid is None:
            app.logger.error("Encryption failed.")
            return None
        if not tokens:
            app.logger.error("No tokens to send requests with.")
            return None

        semaphore = asyncio.Semaphore(LIKE_CONCURRENCY)
        connector = aiohttp.TCPConnector(limit=LIKE_CONCURRENCY)
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)

        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            async def worker(token):
                async with semaphore:
                    return await xpopu_send_request(session, encrypted_uid, token, url)

            results = await asyncio.gather(*(worker(t["token"]) for t in tokens))

        sent = len(results)
        success = sum(1 for ok, _ in results if ok)
        return {"requests_sent": sent, "success": success, "failed": sent - success}
    except Exception as e:
        app.logger.error(f"Exception in send_multiple_requests: {e}")
        return None

def xpopu_create_protobuf(uid):
    try:
        message = uid_generator_pb2.uid_generator()
        message.saturn_ = int(uid)
        message.garena = 1
        return message.SerializeToString()
    except Exception as e:
        app.logger.error(f"Error creating uid protobuf: {e}")
        return None

def xpopu_enc(uid):
    protobuf_data = xpopu_create_protobuf(uid)
    if protobuf_data is None:
        return None
    encrypted_uid = xpopu_encrypt_message(protobuf_data)
    return encrypted_uid

def xpopu_make_request(encrypt, server_name, token):
    try:
        if server_name == "IND":
            url = "https://client.ind.freefiremobile.com/GetPlayerPersonalShow"
        elif server_name in {"BR", "US", "SAC", "NA"}:
            url = "https://client.us.freefiremobile.com/GetPlayerPersonalShow"
        else:
            url = "https://clientbp.ppmainecoonghj.com/GetPlayerPersonalShow"
        edata = bytes.fromhex(encrypt)
        headers = {
            'User-Agent': "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
            'Connection': "Keep-Alive",
            'Accept-Encoding': "gzip",
            'Authorization': f"Bearer {token}",
            'Content-Type': "application/x-www-form-urlencoded",
            'Expect': "100-continue",
            'X-Unity-Version': "2018.4.11f1",
            'X-GA': "v1 1",
            'ReleaseVersion': "OB55"
        }
        response = requests.post(url, data=edata, headers=headers, verify=False, timeout=REQUEST_TIMEOUT)
        hex_data = response.content.hex()
        binary = bytes.fromhex(hex_data)
        decode = xpopu_decode_protobuf(binary)
        if decode is None:
            app.logger.error("Protobuf decoding returned None.")
        return decode
    except Exception as e:
        app.logger.error(f"Error in make_request: {e}")
        return None

def xpopu_decode_protobuf(binary):
    try:
        items = like_count_pb2.Info()
        items.ParseFromString(binary)
        return items
    except DecodeError as e:
        app.logger.error(f"Error decoding Protobuf data: {e}")
        return None
    except Exception as e:
        app.logger.error(f"Unexpected error during protobuf decoding: {e}")
        return None

@app.route('/like', methods=['GET'])
def xpopu_handle_requests():
    uid = request.args.get("uid")
    server_name = request.args.get("server_name", "").upper()
    api_key = (request.args.get("key") or request.headers.get("X-API-Key") or "").strip()

    if not uid or not server_name:
        return jsonify({"error": "UID and server_name are required"}), 400

    # ---------- KEY SYSTEM ----------
    ok, err, status_code, key_info = keys_manager.check_and_consume(api_key, server_name)
    if not ok:
        return jsonify({"error": err}), status_code

    try:
        def xpopu_process_request():
            tokens = xpopu_load_tokens(server_name)
            if not tokens:
                raise Exception(f"No valid tokens found for server {server_name}.")
            token = tokens[0]['token']
            encrypted_uid = xpopu_enc(uid)
            if encrypted_uid is None:
                raise Exception("Encryption of UID failed.")

            before = xpopu_make_request(encrypted_uid, server_name, token)
            if before is None:
                raise Exception("Failed to retrieve initial player info.")
            try:
                jsone = MessageToJson(before)
            except Exception as e:
                raise Exception(f"Error converting 'before' protobuf to JSON: {e}")
            data_before = json.loads(jsone)
            before_like = data_before.get('AccountInfo', {}).get('Likes', 0)
            try:
                before_like = int(before_like)
            except Exception:
                before_like = 0
            app.logger.info(f"Likes before command: {before_like}")

            if server_name == "IND":
                url = "https://client.ind.freefiremobile.com/LikeProfile"
            elif server_name in {"BR", "US", "SAC", "NA"}:
                url = "https://client.us.freefiremobile.com/LikeProfile"
            else:
                url = "https://clientbp.ppmainecoonghj.com/LikeProfile"

            stats = asyncio.run(xpopu_send_multiple_requests(uid, server_name, url, tokens))
            if stats is None:
                raise Exception("Failed to send like requests.")
            app.logger.info(f"Like requests: {stats}")

            after = xpopu_make_request(encrypted_uid, server_name, token)
            if after is None:
                raise Exception("Failed to retrieve player info after like requests.")
            try:
                jsone_after = MessageToJson(after)
            except Exception as e:
                raise Exception(f"Error converting 'after' protobuf to JSON: {e}")
            data_after = json.loads(jsone_after)
            after_like = int(data_after.get('AccountInfo', {}).get('Likes', 0))
            player_uid = int(data_after.get('AccountInfo', {}).get('UID', 0))
            player_name = str(data_after.get('AccountInfo', {}).get('PlayerNickname', ''))
            like_given = after_like - before_like
            status = 1 if like_given != 0 else 2
            result = {
                "LikesGivenByAPI": like_given,
                "LikesafterCommand": after_like,
                "LikesbeforeCommand": before_like,
                "PlayerNickname": player_name,
                "UID": player_uid,
                "TokensLoaded": len(tokens),
                "RequestsSent": stats["requests_sent"],
                "RequestsSuccess": stats["success"],
                "RequestsFailed": stats["failed"],
                "status": status
            }
            return result

        result = xpopu_process_request()
        result["key_info"] = key_info
        return jsonify(result)
    except Exception as e:
        keys_manager.refund(api_key)  # fail hui request ka use wapas
        app.logger.error(f"Error processing request: {e}")
        return jsonify({"error": str(e)}), 500


# ====================== ADMIN (key management) ======================
# ADMIN_SECRET environment variable set karoge tabhi ye endpoints chalenge.
# Header bhejo:  X-Admin-Secret: <ADMIN_SECRET>
def _admin_ok():
    secret = os.environ.get("ADMIN_SECRET", "")
    given = request.headers.get("X-Admin-Secret", "")
    return bool(secret) and hmac.compare_digest(secret, given)


@app.route('/admin/keys', methods=['GET'])
def admin_list_keys():
    if not _admin_ok():
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify(keys_manager.list_keys())


@app.route('/admin/keys', methods=['POST'])
def admin_add_key():
    if not _admin_ok():
        return jsonify({"error": "Unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    try:
        key, cfg = keys_manager.add_key(
            key=body.get("key"),
            owner=body.get("owner", ""),
            days=body.get("days"),
            daily_limit=body.get("daily_limit"),
            regions=body.get("regions"),
        )
    except Exception as e:
        return jsonify({"error": f"Could not save key: {e}"}), 500
    return jsonify({"key": key, **cfg}), 201


@app.route('/admin/keys/<key>', methods=['DELETE'])
def admin_delete_key(key):
    if not _admin_ok():
        return jsonify({"error": "Unauthorized"}), 401
    if keys_manager.remove_key(key):
        return jsonify({"deleted": key})
    return jsonify({"error": "Key not found"}), 404

if __name__ == '__main__':
    app.run(debug=True, use_reloader=False)
