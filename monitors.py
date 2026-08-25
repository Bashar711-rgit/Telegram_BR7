async def _send_alert(
    self,
    data: Dict[str, Any],
    keyword: str,
    text: str,
    msg_hash: str,
    analysis: Dict[str, Any],
    retry_count: int = 0,
):
    """
    retry_count defaults to 0 to preserve the original call signature
    used from _analyze_and_alert. When this is invoked as part of a DLQ
    retry (_retry_send_alert), retry_count is forwarded so that a
    repeated send failure correctly advances toward max_retries instead
    of resetting (mirrors the same fix applied to
    process_event_from_queue).
    """
    if not self._bot_ref: return
    account_name = data.get("account_name", self.account["name"])
    if not await self._bot_ref.rate_limiter.can_proceed(account_name):
        await self._inc_stat("rate_limited"); return
    chat_id = data["chat_id"]; message_id = data["message_id"]; sender_id = data["sender_id"]
    sender_username = data.get("sender_username"); sender_first_name = data.get("sender_first_name")
    sender_last_name = data.get("sender_last_name"); sender_access_hash = data.get("sender_access_hash")
    chat_access_hash = data.get("chat_access_hash"); chat_username = data.get("chat_username")
    display_name = f"{sender_first_name or ''} {sender_last_name or ''}".strip() or f"مستخدم ({sender_id})"
    send_client = await self._resolve_send_client()
    if not send_client:
        logger.error(f"No available client to send alert [{account_name}]"); await self._inc_stat("send_errors"); return
    chat_info = await self._chat_info(send_client, chat_id, message_id, chat_access_hash=chat_access_hash, chat_username=chat_username)
    analysis["msg_hash"] = msg_hash
    sender = {"id": sender_id, "display": display_name, "username": sender_username, "access_hash": sender_access_hash}
    alert_text, buttons = self._build_alert(sender, chat_info, keyword, text, analysis)
    user_media = data.get("media_object")
    async def do_send():
        sent = False
        if user_media is not None:
            try:
                await send_client.send_file(CFG.TARGET_GROUP_ID, file=user_media, caption=alert_text, buttons=buttons, parse_mode="html", link_preview=False)
                sent = True
            except Exception as e: logger.debug(f"User media send failed: {e}")
        if not sent:
            chat_entity = chat_info.get("entity")
            if chat_entity and getattr(chat_entity, 'id', 0) != 0:
                try:
                    result = await send_client.get_profile_photos(chat_entity, limit=1)
                    if result and hasattr(result, 'photos') and len(result.photos) > 0:
                        await send_client.send_file(CFG.TARGET_GROUP_ID, file=result.photos[0], caption=alert_text, parse_mode="html", link_preview=False)
                        sent = True
                except Exception as e:
                    logger.debug(f"Chat photo fallback send failed [{account_name}]: {e}")
        if not sent:
            await send_client.send_message(CFG.TARGET_GROUP_ID, alert_text, buttons=buttons, parse_mode="html", link_preview=False)
    def _retry_payload() -> Dict[str, Any]:
        payload = dict(data)
        payload["_dlq_kind"] = "alert_resend"
        payload["_dlq_keyword"] = keyword
        payload["_dlq_text"] = text
        payload["_dlq_msg_hash"] = msg_hash
        payload["_dlq_analysis"] = analysis
        return payload
    try:
        await self._send_cb.call(do_send)
        safe_keyword = keyword
        if isinstance(safe_keyword, (tuple, list)): safe_keyword = safe_keyword[0] if safe_keyword else ""
        if not isinstance(safe_keyword, str): safe_keyword = str(safe_keyword) if safe_keyword is not None else ""
        # --- التعديل الجديد: تمرير الحقول الإضافية من analysis إلى AlertRecord ---
        reasons = analysis.get("reasons", [])
        if isinstance(reasons, list):
            reasons_str = "; ".join(reasons)
        else:
            reasons_str = str(reasons or "")
        await self.db.add_alert(AlertRecord(
            message_hash=msg_hash,
            chat_id=chat_id,
            sender_id=sender_id,
            account_name=account_name,
            keyword=safe_keyword,
            alert_text=alert_text,
            timestamp=time.time(),
            decision=analysis.get("decision", "accept"),
            confidence=analysis.get("confidence", 0.0),
            reasons=reasons_str,
            intent_verb=analysis.get("intent_verb"),
            academic_object=analysis.get("academic_object"),
            negation_detected=1 if analysis.get("negation_detected") else 0,
            advert_score=analysis.get("advert_score", 0.0),
        ))
        # --- نهاية التعديل ---
        logger.info(f"Alert sent by {account_name} | kw={keyword!r} | sender={display_name}")
    except (FloodWaitError, CircuitBreakerOpen) as e:
        logger.warning(
            f"Alert send throttled [{account_name}] msg_hash={msg_hash}: {type(e).__name__}: {e}"
        )
        await self._dlq.push(_retry_payload(), e, retry_count=retry_count)
        await self._inc_stat("send_errors"); raise
    except Exception as e:
        logger.error(f"Send alert error [{account_name}]: {e} - trying fallback")
        try:
            await send_client.send_message(CFG.TARGET_GROUP_ID, alert_text, buttons=buttons, parse_mode=None, link_preview=False)
        except Exception as fe:
            logger.error(f"Fallback failed [{account_name}]: {fe}")
            await self._inc_stat("send_errors")
            await self._dlq.push(_retry_payload(), fe, retry_count=retry_count)