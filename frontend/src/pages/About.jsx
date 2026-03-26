export default function About() {
  return (
    <div className="min-h-screen bg-black text-white px-4 py-12">
      <div className="max-w-3xl mx-auto">
        {/* Header */}
        <div className="text-center mb-12">
          <div className="w-36 h-36 rounded-full overflow-hidden border-2 border-neutral-700 mb-6 mx-auto shadow-xl shadow-white/5">
            <img src="/jackson.jpeg" alt="Jackson Lee" className="w-full h-full object-cover object-top" />
          </div>
          <h1 className="text-4xl font-bold tracking-tight mb-2">Jackson Lee</h1>
          <p className="text-neutral-500 text-lg">Creator of Epic Fury Stock Analyzer</p>
        </div>

        {/* Bio Card */}
        <div className="bg-neutral-900/50 border border-neutral-800 rounded-2xl p-8 mb-8">
          <h2 className="text-lg font-semibold text-white mb-4 flex items-center gap-2">
            <span className="w-1.5 h-1.5 bg-green-500 rounded-full"></span>
            About Me
          </h2>
          <p className="text-neutral-300 leading-relaxed text-base">
            My name is Jackson Lee, and I am 14 years old. I attend Windward School in Los Angeles.
            I started investing about a year ago and built this website using Claude Code with the
            help of a colleague from my father's company,{' '}
            <span className="text-white font-medium">JRK Property Holdings</span>.
          </p>
        </div>

        {/* Details */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div className="bg-neutral-900/50 border border-neutral-800 rounded-xl p-5 text-center">
            <p className="text-neutral-500 text-xs uppercase tracking-wider mb-2">Location</p>
            <p className="text-white font-medium">Los Angeles, CA</p>
          </div>
          <div className="bg-neutral-900/50 border border-neutral-800 rounded-xl p-5 text-center">
            <p className="text-neutral-500 text-xs uppercase tracking-wider mb-2">School</p>
            <p className="text-white font-medium">Windward School</p>
          </div>
          <div className="bg-neutral-900/50 border border-neutral-800 rounded-xl p-5 text-center">
            <p className="text-neutral-500 text-xs uppercase tracking-wider mb-2">Built With</p>
            <p className="text-white font-medium">Claude Code</p>
          </div>
        </div>

        {/* Footer */}
        <div className="text-center mt-12">
          <p className="text-neutral-600 text-sm">Epic Fury Stock Analyzer © {new Date().getFullYear()}</p>
        </div>
      </div>
    </div>
  )
}
