# bot/client.py - OneBot v11 WebSocket Client
import asyncio, fcntl, json, logging, os, uuid
import websockets
import aiohttp, time

log = logging.getLogger("qqbot")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PID_FILE = os.path.join(_ROOT, "bot.pid")
class OneBotClient:
    def __init__(self, config):
        self.config = config
        self.ws_url = config["ws_url"]
        self.token = config["token"]
        self.bot_qq = config["bot_qq"]
        self._ws = None
        self._pending = {}
        self._dispatcher = None
        self._running = False
        self._event_tasks = set()
        self._session = None
        runtime = config.get("runtime", {})
        self._pid_fd = None
        self._queue_size = int(runtime.get("ws_queue_size", 200))
        self._max_event_tasks = int(runtime.get("max_event_tasks", 8))
        self._api_timeout = int(runtime.get("api_timeout_seconds", 8))
        self._connect_timeout = float(runtime.get("connect_timeout_seconds", 5))
        self._reconnect_max_delay = float(runtime.get("reconnect_max_delay_seconds", 60))
        self._dispatch_sem = asyncio.Semaphore(self._max_event_tasks)
        self._stop_event = asyncio.Event()

    def set_dispatcher(self, dispatcher):
        self._dispatcher = dispatcher

    def _acquire_pid(self):
        pid = os.getpid()
        fd = os.open(PID_FILE, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                with os.fdopen(os.dup(fd), "r", encoding="utf-8") as f:
                    old_pid = f.read().strip() or "unknown"
            except OSError:
                old_pid = "unknown"
            os.close(fd)
            log.warning("Another instance is already running (PID %s). Exiting.", old_pid)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, str(pid).encode("utf-8"))
        self._pid_fd = fd
        return True

    def _release_pid(self):
        if self._pid_fd is None:
            return
        try:
            fcntl.flock(self._pid_fd, fcntl.LOCK_UN)
            os.close(self._pid_fd)
            if os.path.exists(PID_FILE):
                os.remove(PID_FILE)
        except OSError:
            pass
        finally:
            self._pid_fd = None

    async def stop(self):
        self._running = False
        self._stop_event.set()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        await self._cancel_event_tasks()

    async def _cancel_event_tasks(self):
        tasks = [t for t in self._event_tasks if not t.done()]
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5)
        except asyncio.TimeoutError:
            log.warning("Timed out waiting for %d event tasks to stop", len(tasks))

    async def run(self):
        if not self._acquire_pid():
            return

        self._running = True
        self._stop_event.clear()
        self._session = aiohttp.ClientSession()
        try:
            url = self.ws_url
            if self.token:
                sep = "&" if "?" in url else "?"
                url = url + sep + "access_token=" + self.token
            retry_delay = 1

            while self._running:
                try:
                    async with websockets.connect(
                        url,
                        max_size=2 * 1024 * 1024,
                        open_timeout=self._connect_timeout,
                        ping_interval=30,
                        ping_timeout=20,
                        max_queue=32,
                    ) as ws:
                        self._ws = ws
                        retry_delay = 1
                        log.info("Connected to OneBot WS")

                        msg_queue = asyncio.Queue(maxsize=self._queue_size)

                        async def ws_reader():
                            try:
                                async for raw in ws:
                                    await msg_queue.put(raw)
                            except Exception as e:
                                log.error("Reader error: %s", e)
                            finally:
                                try:
                                    msg_queue.put_nowait(None)
                                except asyncio.QueueFull:
                                    try:
                                        msg_queue.get_nowait()
                                    except asyncio.QueueEmpty:
                                        pass
                                    try:
                                        msg_queue.put_nowait(None)
                                    except asyncio.QueueFull:
                                        log.warning("Message queue full while closing reader")

                        reader_task = asyncio.create_task(ws_reader())

                        while self._running:
                            raw = await msg_queue.get()
                            if raw is None:
                                break
                            try:
                                data = json.loads(raw)
                                if "echo" in data:
                                    echo = data["echo"]
                                    if echo in self._pending:
                                        fut = self._pending.pop(echo)
                                        if not fut.done():
                                            fut.set_result(data)
                                    continue

                                pt = data.get("post_type", "")
                                if pt == "meta_event":
                                    continue
                                if len(self._event_tasks) >= self._max_event_tasks * 2:
                                    log.warning("Dropping event because dispatch backlog is high")
                                    continue
                                t = asyncio.create_task(self._dispatch_safe(data))
                                self._event_tasks.add(t)
                                t.add_done_callback(self._event_tasks.discard)

                            except json.JSONDecodeError:
                                log.warning("Invalid JSON: %s", str(raw)[:80])
                            except Exception as e:
                                log.error("Message loop error: %s", e, exc_info=True)

                        reader_task.cancel()
                        try:
                            await reader_task
                        except asyncio.CancelledError:
                            pass

                except websockets.ConnectionClosed as e:
                    log.warning("Connection closed: %s", e)
                except Exception as e:
                    log.warning("Connect error: %s (retry in %ds)", e, retry_delay)
                finally:
                    self._ws = None
                    for echo, fut in list(self._pending.items()):
                        if not fut.done():
                            fut.set_result({"status": "disconnected"})
                    self._pending.clear()

                if self._running:
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=retry_delay)
                    except asyncio.TimeoutError:
                        pass
                    retry_delay = min(retry_delay * 2, self._reconnect_max_delay)

        finally:
            await self._cancel_event_tasks()
            if self._session:
                try:
                    await asyncio.sleep(0.3)
                    await self._session.close()
                except Exception:
                    pass
            self._session = None
            self._release_pid()

    async def _dispatch_safe(self, data):
        async with self._dispatch_sem:
            try:
                if self._dispatcher:
                    await self._dispatcher.dispatch(data)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("Dispatch error: %s", e, exc_info=True)

    async def call(self, action, params=None):
        if self._ws is None:
            return {"status": "failed", "msg": "not connected"}
        echo = str(uuid.uuid4())[:8]
        req = {"action": action, "params": params or {}, "echo": echo}
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending[echo] = fut
        try:
            await self._ws.send(json.dumps(req, ensure_ascii=False))
            result = await asyncio.wait_for(fut, timeout=self._api_timeout)
            return result
        except asyncio.TimeoutError:
            self._pending.pop(echo, None)
            log.warning("API %s -> TIMEOUT", action)
            return {"status": "timeout", "msg": "API call timed out"}
        except Exception as e:
            self._pending.pop(echo, None)
            log.error("API %s error: %s", action, e)
            return {"status": "failed", "msg": str(e)}

    async def send_group_msg(self, group_id, message):
        if isinstance(message, str):
            log.debug("[SEND] group=%s text=%s", group_id, message[:80])
            message = [{"type": "text", "data": {"text": message}}]
        else:
            log.debug("[SEND] group=%s card=%s", group_id, str(message)[:80])
        return await self.call("send_group_msg", {"group_id": group_id, "message": message})

    async def send_private_msg(self, user_id, message):
        if isinstance(message, str):
            message = [{"type": "text", "data": {"text": message}}]
        return await self.call("send_private_msg", {"user_id": user_id, "message": message})

    async def send_msg(self, message_type, user_id=None, group_id=None, message=None):
        if isinstance(message, str):
            message = [{"type": "text", "data": {"text": message}}]
        params = {"message_type": message_type, "message": message or []}
        if user_id:
            params["user_id"] = user_id
        if group_id:
            params["group_id"] = group_id
        return await self.call("send_msg", params)

    async def send_like(self, user_id, times=10):
        return await self.call("send_like", {"user_id": user_id, "times": times})

    async def check_url_safely(self, url):
        return await self.call("check_url_safely", {"url": url})

    async def set_group_kick(self, group_id, user_id, reject_add=False):
        return await self.call("set_group_kick",
                               {"group_id": group_id, "user_id": user_id, "reject_add_request": reject_add})

    async def set_group_ban(self, group_id, user_id, duration=1800):
        return await self.call("set_group_ban", {"group_id": group_id, "user_id": user_id, "duration": duration})

    async def set_group_special_title(self, group_id, user_id, title=""):
        return await self.call("set_group_special_title",
                               {"group_id": group_id, "user_id": user_id, "special_title": title})

    async def get_group_member_info(self, group_id, user_id, no_cache=False):
        params = {"group_id": group_id, "user_id": user_id}
        if no_cache:
            params["no_cache"] = True
        return await self.call("get_group_member_info", params)

    async def get_group_member_list(self, group_id):
        return await self.call("get_group_member_list", {"group_id": group_id})

    async def get_group_info(self, group_id):
        return await self.call("get_group_info", {"group_id": group_id})

    async def get_group_info_ex(self, group_id):
        return await self.call("get_group_info_ex", {"group_id": group_id})

    async def get_group_list(self):
        return await self.call("get_group_list", {})

    async def delete_msg(self, message_id):
        return await self.call("delete_msg", {"message_id": message_id})

    async def mark_msg_as_read(self, message_id):
        return await self.call("mark_msg_as_read", {"message_id": message_id})

    async def mark_group_msg_as_read(self, group_id, message_id=0):
        params = {"group_id": group_id}
        if message_id:
            params["message_id"] = message_id
        return await self.call("mark_group_msg_as_read", params)

    async def mark_all_as_read(self):
        return await self.call("_mark_all_as_read", {})

    async def set_msg_emoji_like(self, message_id, emoji_id):
        return await self.call("set_msg_emoji_like", {
            "message_id": message_id,
            "emoji_id": str(emoji_id),
        })

    async def get_image(self, file):
        try:
            result = await self.call("get_image", {"file": file})
            if result.get("status") == "ok":
                data = result.get("data", {})
                return data.get("file") or data.get("url")
        except Exception:
            pass
        return None

    async def set_group_admin(self, group_id, user_id, enable=True):
        return await self.call("set_group_admin",
                               {"group_id": group_id, "user_id": user_id, "enable": enable})

    async def set_group_card(self, group_id, user_id, card=""):
        return await self.call("set_group_card", {
            "group_id": group_id,
            "user_id": user_id,
            "card": card,
        })

    async def set_group_name(self, group_id, group_name):
        return await self.call("set_group_name", {
            "group_id": group_id,
            "group_name": group_name,
        })

    async def set_group_leave(self, group_id, is_dismiss=False):
        return await self.call("set_group_leave", {
            "group_id": group_id,
            "is_dismiss": is_dismiss,
        })

    async def get_group_honor_info(self, group_id, honor_type="all"):
        return await self.call("get_group_honor_info", {
            "group_id": group_id,
            "type": honor_type,
        })

    async def get_group_at_all_remain(self, group_id):
        return await self.call("get_group_at_all_remain", {"group_id": group_id})

    async def get_group_shut_list(self, group_id):
        return await self.call("get_group_shut_list", {"group_id": group_id})

    async def get_essence_msg_list(self, group_id):
        return await self.call("get_essence_msg_list", {"group_id": group_id})

    async def set_essence_msg(self, message_id):
        return await self.call("set_essence_msg", {"message_id": message_id})

    async def delete_essence_msg(self, message_id):
        return await self.call("delete_essence_msg", {"message_id": message_id})

    async def send_group_notice(self, group_id, content, image=""):
        params = {"group_id": group_id, "content": content}
        if image:
            params["image"] = image
        return await self.call("_send_group_notice", params)

    async def get_group_notice(self, group_id):
        return await self.call("_get_group_notice", {"group_id": group_id})

    async def del_group_notice(self, group_id, notice_id):
        return await self.call("_del_group_notice", {
            "group_id": group_id,
            "notice_id": notice_id,
        })

    async def get_record(self, file, out_format="mp3"):
        return await self.call("get_record", {"file": file, "out_format": out_format})

    async def get_file(self, file_id):
        return await self.call("get_file", {"file_id": file_id})

    async def ocr_image(self, image):
        return await self.call("ocr_image", {"image": image})

    async def ocr_image_enhanced(self, image):
        return await self.call(".ocr_image", {"image": image})

    async def get_forward_msg(self, message_id):
        return await self.call("get_forward_msg", {"message_id": message_id})

    async def send_group_forward_msg(self, group_id, messages):
        return await self.call("send_group_forward_msg", {
            "group_id": group_id,
            "messages": messages,
        })

    async def send_private_forward_msg(self, user_id, messages):
        return await self.call("send_private_forward_msg", {
            "user_id": user_id,
            "messages": messages,
        })

    async def upload_group_file(self, group_id, file, name, folder=""):
        return await self.call("upload_group_file", {
            "group_id": group_id,
            "file": file,
            "name": name,
            "folder": folder,
        })

    async def delete_group_file(self, group_id, file_id, busid):
        return await self.call("delete_group_file", {
            "group_id": group_id,
            "file_id": file_id,
            "busid": busid,
        })

    async def create_group_file_folder(self, group_id, name, parent_id="/"):
        return await self.call("create_group_file_folder", {
            "group_id": group_id,
            "name": name,
            "parent_id": parent_id,
        })

    async def delete_group_folder(self, group_id, folder_id):
        return await self.call("delete_group_folder", {
            "group_id": group_id,
            "folder_id": folder_id,
        })

    async def get_group_file_system_info(self, group_id):
        return await self.call("get_group_file_system_info", {"group_id": group_id})

    async def get_group_root_files(self, group_id):
        return await self.call("get_group_root_files", {"group_id": group_id})

    async def get_group_files_by_folder(self, group_id, folder_id):
        return await self.call("get_group_files_by_folder", {
            "group_id": group_id,
            "folder_id": folder_id,
        })

    async def get_group_file_url(self, group_id, file_id, busid):
        return await self.call("get_group_file_url", {
            "group_id": group_id,
            "file_id": file_id,
            "busid": busid,
        })

    async def move_group_file(self, group_id, file_id, parent_directory, target_directory):
        return await self.call("move_group_file", {
            "group_id": group_id,
            "file_id": file_id,
            "parent_directory": parent_directory,
            "target_directory": target_directory,
        })

    async def trans_group_file(self, group_id, file_id, current_parent_directory, target_group_id, target_directory):
        return await self.call("trans_group_file", {
            "group_id": group_id,
            "file_id": file_id,
            "current_parent_directory": current_parent_directory,
            "target_group_id": target_group_id,
            "target_directory": target_directory,
        })

    async def rename_group_file(self, group_id, file_id, current_parent_directory, new_name):
        return await self.call("rename_group_file", {
            "group_id": group_id,
            "file_id": file_id,
            "current_parent_directory": current_parent_directory,
            "new_name": new_name,
        })

    async def upload_private_file(self, user_id, file, name):
        return await self.call("upload_private_file", {
            "user_id": user_id,
            "file": file,
            "name": name,
        })

    async def get_private_file_url(self, user_id, file_id):
        return await self.call("get_private_file_url", {
            "user_id": user_id,
            "file_id": file_id,
        })

    async def download_file(self, url, thread_count=2, headers=None):
        return await self.call("download_file", {
            "url": url,
            "thread_count": thread_count,
            "headers": headers or [],
        })

    async def set_group_add_request(self, flag, sub_type, approve=True, reason=""):
        return await self.call("set_group_add_request", {
            "flag": flag,
            "sub_type": sub_type,
            "approve": approve,
            "reason": reason,
        })

    async def set_friend_add_request(self, flag, approve=True, remark=""):
        return await self.call("set_friend_add_request", {
            "flag": flag,
            "approve": approve,
            "remark": remark,
        })

    async def get_ai_characters(self, group_id, chat_type=1):
        return await self.call("get_ai_characters", {
            "group_id": group_id,
            "chat_type": chat_type,
        })

    async def get_ai_record(self, group_id, character, text):
        return await self.call("get_ai_record", {
            "group_id": group_id,
            "character": character,
            "text": text,
        })

    async def send_group_ai_record(self, group_id, character, text):
        return await self.call("send_group_ai_record", {
            "group_id": group_id,
            "character": character,
            "text": text,
        })
    async def send_group_msg_reply(self, group_id, message, reply_to_msg_id):
        """Send a group message that replies to a specific message."""
        if isinstance(message, str):
            reply_seg = {"type": "reply", "data": {"id": str(reply_to_msg_id)}}
            text_seg = {"type": "text", "data": {"text": message}}
            full_message = [reply_seg, text_seg]
        else:
            reply_seg = {"type": "reply", "data": {"id": str(reply_to_msg_id)}}
            full_message = [reply_seg] + message
        return await self.call("send_group_msg", {"group_id": group_id, "message": full_message})

    async def send_group_msg_with_at(self, group_id, text, at_qqs):
        """Send a group message with @mentions."""
        segments = []
        for qq in at_qqs:
            segments.append({"type": "at", "data": {"qq": str(qq)}})
        segments.append({"type": "text", "data": {"text": " " + text}})
        return await self.call("send_group_msg", {"group_id": group_id, "message": segments})

    async def get_msg(self, message_id):
        """Get a specific message by ID."""
        return await self.call("get_msg", {"message_id": message_id})

    async def get_group_member_list_cached(self, group_id):
        """Get group member list with caching."""
        cache_key = f"_member_cache_{group_id}"
        if not hasattr(self, "_member_cache"):
            self._member_cache = {}
        cached = self._member_cache.get(cache_key)
        if cached and time.time() - cached.get("ts", 0) < 300:
            return cached["data"]
        result = await self.call("get_group_member_list", {"group_id": group_id})
        if result.get("status") == "ok":
            self._member_cache[cache_key] = {"data": result, "ts": time.time()}
        return result

    async def get_group_msg_history(self, group_id, count=20):
        return await self.call("get_group_msg_history", {"group_id": group_id, "count": count})

    async def set_group_portrait(self, group_id, file, cache=1):
        return await self.call("set_group_portrait", {"group_id": group_id, "file": file, "cache": cache})

    async def get_group_system_msg(self):
        return await self.call("get_group_system_msg", {})

    async def friend_poke(self, user_id):
        return await self.call("friend_poke", {"user_id": user_id})

    async def get_stranger_info(self, user_id, no_cache=False):
        return await self.call("get_stranger_info", {"user_id": user_id, "no_cache": no_cache})

    async def get_profile_like(self):
        return await self.call("get_profile_like", {})

    async def forward_group_single_msg(self, group_id, message_id):
        return await self.call("forward_group_single_msg", {
            "group_id": group_id, "message_id": str(message_id)
        })

    @property
    def session(self):
        return self._session
