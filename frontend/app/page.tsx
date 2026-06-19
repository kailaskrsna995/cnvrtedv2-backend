/**
 * ROOT PAGE
 * Redirects to /dashboard if logged in, /login if not.
 * TODO: wire up Supabase Auth session check
 */
export default function Home() {
  return (
    <main className="min-h-screen flex items-center justify-center bg-black text-white">
      <div className="text-center">
        <h1 className="text-4xl font-bold mb-2">cnvrted</h1>
        <p className="text-zinc-400 mb-8">Find companies ready to buy your service — right now.</p>
        <a href="/onboarding" className="bg-white text-black px-6 py-3 rounded-lg font-medium">
          Get Started
        </a>
      </div>
    </main>
  )
}
