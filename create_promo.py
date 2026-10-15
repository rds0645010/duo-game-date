import os
import sys
import math
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import imageio

if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

WIDTH = 720
HEIGHT = 1280
FPS = 30
DURATION_SEC = 10
TOTAL_FRAMES = FPS * DURATION_SEC

# Paths to system fonts on Windows
FONT_DIR = "C:/Windows/Fonts"
font_bold_path = os.path.join(FONT_DIR, "segoeuib.ttf")
if not os.path.exists(font_bold_path):
    font_bold_path = os.path.join(FONT_DIR, "arialbd.ttf")

font_regular_path = os.path.join(FONT_DIR, "segoeui.ttf")
if not os.path.exists(font_regular_path):
    font_regular_path = os.path.join(FONT_DIR, "arial.ttf")

font_mono_path = os.path.join(FONT_DIR, "consola.ttf")

def get_font(size, bold=True):
    path = font_bold_path if bold else font_regular_path
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()

def get_mono_font(size):
    try:
        return ImageFont.truetype(font_mono_path, size)
    except Exception:
        return get_font(size, bold=True)

# Colors
BG_COLOR = (13, 14, 21)
SURFACE_1 = (21, 23, 34)
SURFACE_2 = (27, 30, 44)
CYAN = (0, 229, 255)
PINK = (255, 92, 138)
LIME = (0, 255, 136)
WHITE = (255, 255, 255)
TEXT_MUTED = (140, 150, 180)
SHADOW_COLOR = (0, 0, 0)

# Pre-load avatar if available
avatar_img = None
avatar_path = "duo_bot_avatar_branded.jpg"
if os.path.exists(avatar_path):
    try:
        im = Image.open(avatar_path).convert("RGBA")
        avatar_img = im
    except Exception as e:
        print(f"Could not load avatar: {e}")

# Pre-generate particle positions
np.random.seed(42)
NUM_PARTICLES = 35
particles = []
for _ in range(NUM_PARTICLES):
    particles.append({
        "x": np.random.uniform(20, WIDTH - 20),
        "y": np.random.uniform(20, HEIGHT - 20),
        "speed": np.random.uniform(0.6, 2.2),
        "size": np.random.uniform(2.5, 6.0),
        "color": CYAN if np.random.rand() > 0.4 else PINK,
        "alpha": np.random.uniform(0.3, 0.8)
    })

def draw_rounded_card(draw, box, radius=16, fill=SURFACE_1, outline=None, width=2, shadow=True, shadow_offset=(5, 5)):
    x0, y0, x1, y1 = box
    width = int(width)
    radius = int(radius)
    if shadow:
        sx, sy = shadow_offset
        draw.rounded_rectangle([x0 + sx, y0 + sy, x1 + sx, y1 + sy], radius=radius, fill=SHADOW_COLOR)
    draw.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=fill, outline=outline, width=width)

def draw_header_badge(draw, text, y=140, bg=SURFACE_2, border=CYAN, text_color=CYAN):
    font = get_mono_font(20)
    bbox = font.getbbox(text)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    px, py = 16, 7
    bx0 = (WIDTH - tw) // 2 - px
    by0 = y - py
    bx1 = bx0 + tw + px * 2
    by1 = by0 + th + py * 2
    draw_rounded_card(draw, [bx0, by0, bx1, by1], radius=10, fill=bg, outline=border, width=2, shadow=True, shadow_offset=(3, 3))
    draw.text(((WIDTH - tw) // 2, y - 2), text, font=font, fill=text_color)

def draw_progress_bar(draw, frame_idx):
    # Top progress line
    progress = frame_idx / float(TOTAL_FRAMES)
    bar_w = int(WIDTH * progress)
    # Background strip
    draw.rectangle([0, 0, WIDTH, 8], fill=(25, 28, 42))
    # Filled strip gradient
    draw.rectangle([0, 0, bar_w, 8], fill=CYAN)
    if bar_w > 10:
        draw.rectangle([max(0, bar_w - 40), 0, bar_w, 8], fill=PINK)

    # Time pill at top right
    time_sec = frame_idx / FPS
    time_str = f"00:{int(time_sec):02d} / 00:10"
    font = get_mono_font(18)
    draw.text((WIDTH - 150, 20), time_str, font=font, fill=TEXT_MUTED)
    draw.text((30, 20), "👾 DUO // REEL", font=font, fill=CYAN)

def draw_grid_background(draw, frame_idx):
    # Slow animated grid
    offset = (frame_idx * 1.5) % 40
    for y in range(int(offset), HEIGHT, 40):
        draw.line([(0, y), (WIDTH, y)], fill=(20, 22, 33), width=1)
    for x in range(0, WIDTH, 40):
        draw.line([(x, 0), (x, HEIGHT)], fill=(20, 22, 33), width=1)

def draw_particles(draw, frame_idx):
    for p in particles:
        py = (p["y"] - frame_idx * p["speed"]) % HEIGHT
        px = p["x"] + math.sin(frame_idx * 0.05 + p["x"]) * 8
        r = p["size"]
        color = p["color"]
        draw.ellipse([px - r, py - r, px + r, py + r], fill=color)

def render_frame(frame_idx):
    img = Image.new("RGB", (WIDTH, HEIGHT), BG_COLOR)
    draw = ImageDraw.Draw(img)

    draw_grid_background(draw, frame_idx)
    draw_particles(draw, frame_idx)
    draw_progress_bar(draw, frame_idx)

    time_sec = frame_idx / FPS

    # ============================================================
    # SCENE 1: HOOK (0.0s - 2.5s, frames 0 - 75)
    # Problem: "Не с кем поиграть? Вечные споры 'во что'?"
    # ============================================================
    if time_sec < 2.5:
        # Fade in
        alpha = min(1.0, frame_idx / 15.0)
        draw_header_badge(draw, "🕹️ ЗНАКОМАЯ СИТУАЦИЯ?", y=150, border=PINK, text_color=PINK)

        font_large = get_font(52, bold=True)
        t1 = "Вечно не с кем"
        t2 = "покатать в кайф?"
        w1 = font_large.getbbox(t1)[2] - font_large.getbbox(t1)[0]
        w2 = font_large.getbbox(t2)[2] - font_large.getbbox(t2)[0]
        draw.text(((WIDTH - w1) // 2, 230), t1, font=font_large, fill=WHITE)
        draw.text(((WIDTH - w2) // 2, 295), t2, font=font_large, fill=PINK)

        # 3 Frustrating Chat Bubble cards
        bubbles = [
            ("👤 Друг 1:", "«Бро, я сегодня занят... хз когда буду»", (220, 70, 70), 400),
            ("👤 Друг 2:", "«Да во что играть? Я не знаю...»", (255, 170, 50), 520),
            ("👤 Беседа:", "«*Чаты молчат уже 3 дня* 🦗»", (160, 160, 180), 640)
        ]

        for i, (who, text, col, by) in enumerate(bubbles):
            delay = i * 14
            if frame_idx >= delay:
                # Spring pop in
                scale_anim = min(1.0, (frame_idx - delay) / 10.0)
                card_w = int(620 * scale_anim)
                card_h = 95
                cx0 = (WIDTH - card_w) // 2
                cy0 = by
                cx1 = cx0 + card_w
                cy1 = cy0 + card_h
                if card_w > 100:
                    draw_rounded_card(draw, [cx0, cy0, cx1, cy1], radius=16, fill=SURFACE_1, outline=(50, 55, 80), width=2)
                    f_who = get_font(20, bold=True)
                    f_txt = get_font(22, bold=False)
                    draw.text((cx0 + 20, cy0 + 16), who, font=f_who, fill=col)
                    draw.text((cx0 + 20, cy0 + 46), text, font=f_txt, fill=WHITE)

        # Big punchline at bottom
        if frame_idx >= 40:
            f_sub = get_font(30, bold=True)
            p_text = "ХВАТИТ ЭТО ТЕРПЕТЬ! 🛑"
            pw = f_sub.getbbox(p_text)[2] - f_sub.getbbox(p_text)[0]
            draw.text(((WIDTH - pw) // 2, 800), p_text, font=f_sub, fill=CYAN)

            f_hint = get_font(24, bold=False)
            h_text = "Есть решение за 2 клика 👇"
            hw = f_hint.getbbox(h_text)[2] - f_hint.getbbox(h_text)[0]
            draw.text(((WIDTH - hw) // 2, 850), h_text, font=f_hint, fill=TEXT_MUTED)

    # ============================================================
    # SCENE 2: INTRODUCING DUO (2.5s - 5.0s, frames 75 - 150)
    # Solution: DUO Matchmaking
    # ============================================================
    elif time_sec < 5.0:
        local_frame = frame_idx - 75
        draw_header_badge(draw, "✨ РЕШЕНИЕ НАЙДЕНО // DUO", y=140, border=LIME, text_color=LIME)

        f_hero = get_font(56, bold=True)
        t1 = "Встречай DUO 🎮"
        w1 = f_hero.getbbox(t1)[2] - f_hero.getbbox(t1)[0]
        draw.text(((WIDTH - w1) // 2, 215), t1, font=f_hero, fill=WHITE)

        f_sub = get_font(25, bold=False)
        sub_t = "Игровые свидания и катки в 2 клика"
        sw = f_sub.getbbox(sub_t)[2] - f_sub.getbbox(sub_t)[0]
        draw.text(((WIDTH - sw) // 2, 285), sub_t, font=f_sub, fill=CYAN)

        # Game choice chips
        games = [
            ("🧱 Roblox", "Чилл & плейсы", CYAN, 360),
            ("🎯 CS2 / Dota 2", "Потный ранкед", PINK, 480),
            ("⛏️ Minecraft", "Уютное выживание", LIME, 600)
        ]

        for i, (g_title, g_vibe, g_color, gy) in enumerate(games):
            g_delay = i * 10
            if local_frame >= g_delay:
                bounce = math.sin(min(math.pi / 2, (local_frame - g_delay) / 8.0))
                card_w = int(620 * bounce)
                cx0 = (WIDTH - card_w) // 2
                cy0 = gy
                cx1 = cx0 + card_w
                cy1 = cy0 + 100
                if card_w > 50:
                    draw_rounded_card(draw, [cx0, cy0, cx1, cy1], radius=16, fill=SURFACE_1, outline=g_color, width=3)
                    # Game title
                    draw.text((cx0 + 24, cy0 + 18), g_title, font=get_font(30, bold=True), fill=WHITE)
                    # Vibe chip
                    draw.text((cx0 + 24, cy0 + 58), g_vibe, font=get_font(20, bold=False), fill=TEXT_MUTED)
                    # Selected radio
                    draw.ellipse([cx1 - 50, cy0 + 35, cx1 - 22, cy0 + 63], fill=g_color)
                    draw.text((cx1 - 42, cy0 + 38), "✓", font=get_font(18, bold=True), fill=(0, 0, 0))

        # Bottom feature bullets
        if local_frame >= 35:
            f_foot = get_font(23, bold=True)
            txt_foot = "⚡ Время, игра и вайб выбираются на лету!"
            fw = f_foot.getbbox(txt_foot)[2] - f_foot.getbbox(txt_foot)[0]
            draw.text(((WIDTH - fw) // 2, 760), txt_foot, font=f_foot, fill=WHITE)

    # ============================================================
    # SCENE 3: THE DUO PASS TICKET & 1-TAP ACCEPT (5.0s - 7.5s, frames 150 - 225)
    # Magic: Arcade ticket with laser and interactive bot card
    # ============================================================
    elif time_sec < 7.5:
        local_frame = frame_idx - 150
        draw_header_badge(draw, "🎟️ ТАКТИЛЬНЫЙ DUO PASS", y=120, border=CYAN, text_color=CYAN)

        # Ticket Card drawing
        tx0, ty0, tx1, ty1 = 60, 180, WIDTH - 60, 680
        # Draw ticket with punch holes & tear notches
        draw_rounded_card(draw, [tx0, ty0, tx1, ty1], radius=20, fill=(18, 20, 30), outline=(50, 60, 90), width=3)

        # Ticket top strip
        draw.rectangle([tx0 + 2, ty0 + 2, tx1 - 2, ty0 + 65], fill=SURFACE_2)
        draw.text((tx0 + 24, ty0 + 20), "CO-OP PASS // INSERT COIN", font=get_mono_font(18), fill=CYAN)
        draw.text((tx1 - 130, ty0 + 20), "● P1 READY", font=get_mono_font(18), fill=LIME)
        draw.line([(tx0, ty0 + 65), (tx1, ty0 + 65)], fill=(60, 70, 100), width=2)

        # Ticket items
        rows = [
            ("🕹 Игра:", "Roblox (Плейсы & Чилл)", WHITE),
            ("👤 Адресат:", "Любимый тиммейт 💕", PINK),
            ("📅 Тайминг:", "Сегодня в 20:00", CYAN),
            ("✨ Вайб:", "🍵 Уютный чилл (1-2 часа)", LIME)
        ]
        ry = ty0 + 90
        for label, val, c in rows:
            draw.text((tx0 + 26, ry), label, font=get_font(20, bold=False), fill=TEXT_MUTED)
            draw.text((tx0 + 170, ry), val, font=get_font(22, bold=True), fill=c)
            ry += 52

        # Barcode with laser scanner!
        by = ty0 + 320
        draw.line([(tx0 + 20, by), (tx1 - 20, by)], fill=(40, 48, 70), width=2)
        draw.text((tx0 + 60, by + 18), "|||| | ||||| || |||||| | |||| | ||", font=get_mono_font(28), fill=(140, 150, 180))

        # Animated laser beam
        laser_x = tx0 + 30 + int((local_frame * 12) % (tx1 - tx0 - 60))
        draw.line([(laser_x, by + 10), (laser_x, by + 58)], fill=PINK, width=4)

        # Simulation of Telegram Bot Interactive Message
        if local_frame >= 25:
            # Pop-in Telegram Card below ticket
            pop_alpha = min(1.0, (local_frame - 25) / 10.0)
            mx0, my0, mx1, my1 = 50, 730, WIDTH - 50, 940
            draw_rounded_card(draw, [mx0, my0, mx1, my1], radius=16, fill=(24, 30, 48), outline=CYAN, width=2)

            draw.text((mx0 + 20, my0 + 16), "🤖 @duo_play_date_bot доставил инвайт:", font=get_font(19, bold=True), fill=CYAN)
            draw.text((mx0 + 20, my0 + 46), "«Тебя зовут в катку в DUO! Ты в деле?»", font=get_font(21, bold=False), fill=WHITE)

            # Interactive button simulation
            # Green Accepted button
            btn_col = LIME if local_frame >= 45 else (35, 45, 65)
            btn_txt_col = (0, 0, 0) if local_frame >= 45 else WHITE
            draw_rounded_card(draw, [mx0 + 20, my0 + 90, mx1 - 20, my0 + 150], radius=12, fill=btn_col, outline=LIME, width=2)
            draw.text((mx0 + 120, my0 + 107), "✅ Я в деле! Погнали! 🚀", font=get_font(24, bold=True), fill=btn_txt_col)

            if local_frame >= 50:
                # Instant notification badge
                draw_rounded_card(draw, [70, 970, WIDTH - 70, 1050], radius=12, fill=(10, 45, 25), outline=LIME, width=2)
                draw.text((100, 985), "🎉 ТИММЕЙТ СОГЛАСИЛСЯ! ЗАПУСКАЙ!", font=get_font(22, bold=True), fill=LIME)

    # ============================================================
    # SCENE 4: CALL TO ACTION (7.5s - 10.0s, frames 225 - 300)
    # Finish with branded logo and viral call to action
    # ============================================================
    else:
        local_frame = frame_idx - 225
        # Confetti bursts
        for _ in range(8):
            cx = (WIDTH // 2) + math.cos(local_frame * 0.4 + _) * (150 + local_frame * 3)
            cy = 380 + math.sin(local_frame * 0.5 + _) * (120 + local_frame * 2)
            draw.rectangle([cx, cy, cx + 12, cy + 12], fill=PINK if _ % 2 == 0 else CYAN)

        # Avatar or Big DUO Logo
        if avatar_img:
            # Resize avatar
            av_size = 200
            av_resized = avatar_img.resize((av_size, av_size), Image.Resampling.LANCZOS)
            # Create circular mask
            mask = Image.new("L", (av_size, av_size), 0)
            m_draw = ImageDraw.Draw(mask)
            m_draw.ellipse([0, 0, av_size, av_size], fill=255)

            av_x = (WIDTH - av_size) // 2
            av_y = 160
            # Neon border circle
            pulse_rad = int(av_size // 2 + 8 + math.sin(local_frame * 0.2) * 5)
            draw.ellipse([WIDTH // 2 - pulse_rad, av_y + av_size // 2 - pulse_rad,
                          WIDTH // 2 + pulse_rad, av_y + av_size // 2 + pulse_rad],
                         outline=CYAN, width=4)
            img.paste(av_resized, (av_x, av_y), mask)
        else:
            draw.ellipse([WIDTH // 2 - 100, 160, WIDTH // 2 + 100, 360], fill=SURFACE_1, outline=CYAN, width=4)
            draw.text((WIDTH // 2 - 50, 230), "DUO", font=get_font(50, bold=True), fill=PINK)

        # Big Title
        f_brand = get_font(58, bold=True)
        bt = "DUO"
        bw = f_brand.getbbox(bt)[2] - f_brand.getbbox(bt)[0]
        draw.text(((WIDTH - bw) // 2, 400), bt, font=f_brand, fill=WHITE)

        f_claim = get_font(28, bold=True)
        c_text = "Твой сервис для совместных игр"
        cw = f_claim.getbbox(c_text)[2] - f_claim.getbbox(c_text)[0]
        draw.text(((WIDTH - cw) // 2, 480), c_text, font=f_claim, fill=CYAN)

        # 3 Value props
        props = [
            "⚡ Зови тиммейта в один клик",
            "🎮 5 популярных стартовых игр",
            "🔔 Мгновенные уведомления в Telegram"
        ]
        py = 560
        for p in props:
            pw = get_font(23, bold=False).getbbox(p)[2] - get_font(23, bold=False).getbbox(p)[0]
            draw.text(((WIDTH - pw) // 2, py), p, font=get_font(23, bold=False), fill=WHITE)
            py += 46

        # Big glowing CTA Button
        btn_y = 740
        pulse = math.sin(local_frame * 0.25) * 4
        bx0, by0, bx1, by1 = 70 - int(pulse), btn_y, WIDTH - 70 + int(pulse), btn_y + 85
        draw_rounded_card(draw, [bx0, by0, bx1, by1], radius=18, fill=CYAN, outline=WHITE, width=3, shadow=True)

        cta_txt = "🚀 ЗАПУСКАЙ @duo_play_date_bot"
        f_cta = get_font(26, bold=True)
        ctaw = f_cta.getbbox(cta_txt)[2] - f_cta.getbbox(cta_txt)[0]
        draw.text(((WIDTH - ctaw) // 2, btn_y + 24), cta_txt, font=f_cta, fill=(5, 12, 18))

        # Subtext
        f_sub = get_font(21, bold=False)
        sub_t = "Бесплатно прямо внутри Telegram Mini Apps"
        sw = f_sub.getbbox(sub_t)[2] - f_sub.getbbox(sub_t)[0]
        draw.text(((WIDTH - sw) // 2, 855), sub_t, font=f_sub, fill=TEXT_MUTED)

    return np.array(img)

def main():
    output_mp4 = "duo_explainer_10s.mp4"
    output_gif = "duo_explainer_10s.gif"

    print(f"🎬 Рендерим 10-секундный ролик ({TOTAL_FRAMES} кадров, {WIDTH}x{HEIGHT}, {FPS} FPS)...")

    writer_mp4 = imageio.get_writer(output_mp4, fps=FPS, codec='libx264', quality=8, pixelformat='yuv420p')

    # Also sample frames for animated GIF (15 FPS, half-scale 360x640 for lightweight preview)
    gif_frames = []

    for f in range(TOTAL_FRAMES):
        frame_rgb = render_frame(f)
        writer_mp4.append_data(frame_rgb)

        # Sample every 2nd frame for 15 FPS GIF
        if f % 2 == 0:
            pil_f = Image.fromarray(frame_rgb).resize((360, 640), Image.Resampling.BILINEAR)
            gif_frames.append(np.array(pil_f))

        if (f + 1) % 60 == 0 or f == TOTAL_FRAMES - 1:
            print(f"  Прогресс: {f + 1}/{TOTAL_FRAMES} кадров ({(f + 1)/TOTAL_FRAMES*100:.0f}%)")

    writer_mp4.close()
    print(f"✅ Видео сохранено: {output_mp4} ({os.path.getsize(output_mp4) / 1024 / 1024:.2f} MB)")

    print("🎬 Сохраняем анимированный GIF превью...")
    imageio.mimsave(output_gif, gif_frames, fps=15, loop=0)
    print(f"✅ GIF сохранен: {output_gif} ({os.path.getsize(output_gif) / 1024 / 1024:.2f} MB)")

if __name__ == "__main__":
    main()
