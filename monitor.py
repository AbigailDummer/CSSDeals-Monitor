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


def fetch_page(config: dict, page: int) -> list:
    url = f"{config['api_base']}/api/product"
    params = {
        "fields": 1,
        "categoryId": config.get("category_id", ""),
        "page": page,
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


def fetch_new_products(config: dict, seen_ids: set) -> list:
    """Percorre as páginas (da mais nova para a mais antiga) até encontrar uma
    página com produtos já vistos, garantindo que nenhum produto novo seja
    perdido mesmo que muitos tenham sido adicionados desde a última checagem.

    Cada página é lida até o fim (não para no meio ao achar o primeiro
    conhecido), e depois da primeira página com algum produto conhecido ainda
    é buscada mais uma página extra de margem — assim, mesmo que um produto
    novo não apareça estritamente no topo da lista, ele ainda é capturado."""
    max_pages = config.get("max_pages_per_check", 20)
    new_products = []
    page = 1
    safety_margin_left = 1
    while page <= max_pages:
        products = fetch_page(config, page)
        if not products:
            break

        page_had_known = False
        for p in products:
            if p["id"] in seen_ids:
                page_had_known = True
            else:
                new_products.append(p)

        if page_had_known:
            if safety_margin_left <= 0:
                break
            safety_margin_left -= 1
        page += 1
    else:
        log.warning(
            "Atingiu o limite de %d página(s) sem encontrar um produto já visto — "
            "pode haver produtos novos além do que foi verificado nesta execução.",
            max_pages,
        )

    return new_products


def send_discord_message(webhook_url: str, content: str = None, embeds: list = None) -> None:
    payload = {}
    if content:
        payload["content"] = content
    if embeds:
        payload["embeds"] = embeds

    resp = requests.post(webhook_url, json=payload, timeout=15)
    if resp.status_code == 429:
        retry_after = resp.json().get("retry_after", 1)
        log.warning("Rate limit do Discord atingido, aguardando %.1fs.", retry_after)
        time.sleep(retry_after)
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
        if i + 10 < len(embeds):
            time.sleep(1)  # evita rate limit do Discord em rajadas grandes


def run_check(config: dict, state_path: Path, webhook_url: str) -> None:
    state = load_state(state_path)
    seen_ids = set(state.get("seen_ids", []))

    if not seen_ids:
        products = fetch_page(config, 1)
        log.info("Primeira execução: salvando %d produto(s) como estado inicial (sem notificar).", len(products))
        state["seen_ids"] = [p["id"] for p in products]
        save_state(state_path, state)
        send_discord_message(
            webhook_url,
            content=f"✅ Monitor iniciado para {config['api_base']} — {len(products)} produto(s) na base inicial.",
        )
        return

    new_products = fetch_new_products(config, seen_ids)
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
        log.info("Nenhum produto novo.")


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
