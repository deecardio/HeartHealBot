'use client';

import { useState, useEffect } from 'react';

export default function Home() {
  const [playerCount, setPlayerCount] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [isSuccess, setIsSuccess] = useState(false);

  // Проверяем, вернулся ли пользователь после успешной оплаты
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get('success') === 'true') {
      setIsSuccess(true);
    }

    // Загружаем актуальное число игроков
    fetch('/api/stats')
      .then((res) => res.json())
      .then((data) => setPlayerCount(data.count))
      .catch((err) => console.error(err));
  }, []);

  const handlePay = async () => {
    try {
      setLoading(true);
      // Звуковой или вибро-отклик, если поддерживается
      if (typeof window !== 'undefined' && 'vibrate' in navigator) {
        navigator.vibrate(25);
      }

      const res = await fetch('/api/checkout', { method: 'POST' });
      const data = await res.json();

      if (data.url) {
        // Перенаправляем на нативную шторку оплаты Stripe
        window.location.href = data.url;
      } else {
        alert('Ошибка инициализации шлюза. Проверь консоль.');
        setLoading(false);
      }
    } catch (err) {
      console.error(err);
      setLoading(false);
    }
  };

  return (
    <main className="min-h-screen bg-black text-white flex flex-col justify-between items-center p-8 select-none font-sans">
      {/* Верхний счетчик */}
      <header className="w-full flex justify-between items-center text-xs tracking-widest text-zinc-500 uppercase font-mono">
        <div className="flex items-center gap-2">
          <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
          <span>System Active</span>
        </div>
        <div>
          Players: <span className="text-zinc-200">{playerCount !== null ? playerCount : '—'}</span>
        </div>
      </header>

      {/* Центральный блок */}
      <div className="flex flex-col items-center justify-center gap-6">
        {isSuccess ? (
          <div className="text-center space-y-4 animate-fade-in">
            <p className="text-xs tracking-[0.3em] text-emerald-400 font-mono uppercase">
              Transaction Verified
            </p>
            <h1 className="text-4xl md:text-5xl font-black tracking-tight text-white">
              PLAYER PASS ISSUED
            </h1>
            <p className="text-zinc-500 text-sm max-w-xs mx-auto">
              You own nothing. You proved everything.
            </p>
          </div>
        ) : (
          <>
            <button
              onClick={handlePay}
              disabled={loading}
              className={`
                relative px-12 py-5 rounded-full font-mono text-sm tracking-[0.25em] uppercase font-bold
                transition-all duration-200 active:scale-95
                ${
                  loading
                    ? 'bg-zinc-800 text-zinc-500 cursor-not-allowed'
                    : 'bg-white text-black hover:bg-zinc-200 shadow-[0_0_50px_rgba(255,255,255,0.15)]'
                }
              `}
            >
              {loading ? 'Routing...' : 'BLANK.'}
            </button>
            <span className="text-[11px] tracking-[0.2em] uppercase text-zinc-600 font-mono">
              Become a player · $1.00
            </span>
          </>
        )}
      </div>

      {/* Нижний футер с манифестом */}
      <footer className="text-center space-y-1">
        <p className="text-[10px] tracking-widest text-zinc-600 uppercase font-mono">
          Pure transaction · No product · Absolute zero
        </p>
      </footer>
    </main>
  );
}
