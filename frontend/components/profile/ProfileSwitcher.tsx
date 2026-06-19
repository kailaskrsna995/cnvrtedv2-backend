/**
 * PROFILE SWITCHER
 * ================
 * Dropdown/modal shown when user clicks their profile name in the top bar.
 * Shows all their profiles + a "New Profile" button.
 *
 * TODO (intern):
 *   1. Accept profiles[] and activeProfileId as props
 *   2. Render list of profiles as clickable rows
 *   3. Active profile has a checkmark
 *   4. Clicking a profile calls onSwitch(profile.id)
 *   5. "New Profile" button links to /onboarding
 */

import { Profile } from "@/lib/types"

interface Props {
  profiles: Profile[]
  activeProfileId: string | null
  onSwitch: (profileId: string) => void
  onClose: () => void
}

export default function ProfileSwitcher({ profiles, activeProfileId, onSwitch, onClose }: Props) {
  return (
    <div className="absolute top-14 left-0 bg-zinc-900 border border-zinc-800 rounded-xl shadow-xl w-64 z-50">

      <div className="p-3 border-b border-zinc-800">
        <p className="text-zinc-400 text-xs uppercase tracking-widest">Your Profiles</p>
      </div>

      <div className="p-2">
        {profiles.map(profile => (
          <button
            key={profile.id}
            onClick={() => { onSwitch(profile.id); onClose() }}
            className="w-full text-left px-3 py-2.5 rounded-lg hover:bg-zinc-800 flex items-center justify-between"
          >
            <div>
              <p className="text-white text-sm font-medium">{profile.name}</p>
              <p className="text-zinc-500 text-xs truncate">{profile.service_description}</p>
            </div>
            {profile.id === activeProfileId && (
              <span className="text-green-400 text-xs">✓</span>
            )}
          </button>
        ))}
      </div>

      <div className="p-2 border-t border-zinc-800">
        <a
          href="/onboarding"
          className="block w-full text-center px-3 py-2 text-zinc-400 hover:text-white text-sm rounded-lg hover:bg-zinc-800"
        >
          + New Profile
        </a>
      </div>

    </div>
  )
}
