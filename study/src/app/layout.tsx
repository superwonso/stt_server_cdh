import type { Metadata, Viewport } from 'next';
import localFont from 'next/font/local';
import './globals.css';
const noto = localFont({
  src: '../fonts/NotoSansKR-ui.woff2',
  display: 'swap',
  preload: false,
  variable: '--font-noto',
  fallback: ['Noto Sans KR', 'Malgun Gothic', 'Apple SD Gothic Neo', 'sans-serif'],
});
export const metadata: Metadata = {
  title: '암기 학습 · 여백',
  description: '일곱 문제씩, 내 속도로 익히는 암기 학습',
  robots: { index: false, follow: false },
};
export const viewport: Viewport = {
  width: 'device-width',
  initialScale: 1,
  viewportFit: 'cover',
  themeColor: '#f6f5ef',
};
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="ko" className={noto.variable}>
      <body>{children}</body>
    </html>
  );
}
