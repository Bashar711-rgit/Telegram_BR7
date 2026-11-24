import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'BR7 Control Plane',
  description: 'لوحة تحكم موحدة: GitHub + Render + Supabase + Vercel — Telegram BR7',
  robots: { index: false, follow: false },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ar" dir="rtl">
      <body className="font-sans">{children}</body>
    </html>
  );
}
