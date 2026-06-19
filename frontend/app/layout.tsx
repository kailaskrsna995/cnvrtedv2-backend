import type { Metadata } from "next"
import "./globals.css"

export const metadata: Metadata = {
  title: "cnvrted",
  description: "Find companies ready to buy your service — right now.",
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="bg-black text-white antialiased">{children}</body>
    </html>
  )
}
