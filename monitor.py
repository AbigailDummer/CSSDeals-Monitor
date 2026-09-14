import argparse
import html
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("cssdeals-monitor")


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def get_webhook_url() -> str:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        log.error(
            "Defina a variável de ambiente DISCORD_WEBHOOK_URL com a URL do webhook do Discord antes de rodar o monitor."
        )
        sys.exit(1)
    return webhook_url


def load_state(state_path: Path) -> dict:
    if state_path.exists():
        with open(state_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"seen_ids": []}


def save_state(state_path: Path, state: dict) -> None:
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fetch_products(config: dict) -> list:
    url = f"{config['api_base']}/api/product"
    params = {
        "fields": 1,
        "categoryId": config.get("category_id", ""),
        "page": 1,
        "pageSize": config.get("page_size", 50),
        "priceMin": config.get("price_min", "0.00"),
        "priceMax": config.get("price_max", "99999.00"),
    }
    response = requests.get(
        url,
        params=params,
        headers={"User-Agent": config["user_agent"]},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != 0:
        raise RuntimeError(f"API retornou erro: {payload.get('msg')}")

    records = payload.get("data", {}).get("records") or []
    products = []
    for r in records:
        sku = (r.get("skus") or [{}])[0]
        pid = str(r.get("id"))
        title = html.unescape(r.get("title") or "")
        price = sku.get("price")
        image = sku.get("image")
        source_link = r.get("sourceLink")
        detail_link = f"{config['api_base']}/product-detail.html?itemid={pid}"
        products.append(
            {
                "id": pid,
                "title": title,
                "price": price,
                "image": image,
                "source_link": source_link,
                "detail_link": detail_link,
            }
        )
    return products


def send_discord_message(webhook_url: str, content: str = None, embeds: list = None) -> None:
    payload = {}
    if content:
        payload["content"] = content
    if embeds:
        payload["embeds"] = embeds
    resp = requests.post(webhook_url, json=payload, timeout=15)
    if resp.status_code >= 300:
        log.error("Falha ao enviar mensagem para o Discord (%s): %s", resp.status_code, resp.text)


def notify_new_products(webhook_url: str, new_products: list) -> None:
    embeds = []
    for p in new_products:
        description_lines = []
        if p["price"] is not None:
            description_lines.append(f"Preço: ¥{p['price']}")
        if p["source_link"]:
            description_lines.append(f"[Link original]({p['source_link']})")

        embed = {
            "title": p["title"][:250] if p["title"] else "Novo produto",
            "url": p["detail_link"],
            "description": "\n".join(description_lines) or None,
            "image": {"url": p["image"]} if p["image"] else None,
            "color": 0x2ECC71,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        embed = {k: v for k, v in embed.items() if v is not None}
        embeds.append(embed)

    for i in range(0, len(embeds), 10):
        chunk = embeds[i : i + 10]
        content = "🆕 Novo(s) produto(s) detectado(s) no CSSDeals!" if i == 0 else None
        send_discord_message(webhook_url, content=content, embeds=chunk)


def run_check(config: dict, state_path: Path, webhook_url: str) -> None:
    products = fetch_products(config)
    state = load_state(state_path)
    seen_ids = set(state.get("seen_ids", []))

    if not seen_ids:
        log.info("Primeira execução: salvando %d produto(s) como estado inicial (sem notificar).", len(products))
        state["seen_ids"] = [p["id"] for p in products]
        save_state(state_path, state)
        send_discord_message(
            webhook_url,
            content=f"✅ Monitor iniciado para {config['api_base']} — {len(products)} produto(s) na base inicial.",
        )
        return

    new_products = [p for p in products if p["id"] not in seen_ids]
    if new_products:
        new_products.reverse()  # notifica do mais antigo para o mais novo
        log.info("Detectado(s) %d produto(s) novo(s).", len(new_products))
        notify_new_products(webhook_url, new_products)

        order = state.get("seen_ids", [])
        order.extend(p["id"] for p in new_products)
        max_seen = config.get("max_seen_ids", 3000)
        if len(order) > max_seen:
            order = order[-max_seen:]
        state["seen_ids"] = order
        save_state(state_path, state)
    else:
        log.info("Nenhum produto novo. Última verificação: %d produto(s) na primeira página.", len(products))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--once",
        action="store_true",
        help="Executa uma única verificação e encerra (usado pelo GitHub Actions).",
    )
    args = parser.parse_args()

    config = load_config()
    webhook_url = get_webhook_url()
    state_path = BASE_DIR / config["state_file"]

    if args.once:
        try:
            run_check(config, state_path, webhook_url)
        except requests.RequestException as exc:
            log.error("Erro ao acessar a API: %s", exc)
            sys.exit(1)
        return

    interval = config["check_interval_seconds"]
    log.info("Monitorando %s/api/product a cada %ds. Pressione Ctrl+C para parar.", config["api_base"], interval)

    while True:
        try:
            run_check(config, state_path, webhook_url)
        except requests.RequestException as exc:
            log.error("Erro ao acessar a API: %s", exc)
        except Exception:
            log.exception("Erro inesperado durante a verificação.")

        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            log.info("Monitor interrompido pelo usuário.")
            break


if __name__ == "__main__":
    main()
