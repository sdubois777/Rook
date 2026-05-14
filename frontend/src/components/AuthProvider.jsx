import { useEffect } from 'react'
import { useAuth } from '@clerk/clerk-react'
import { registerTokenGetter } from '../api/client'

/**
 * Registers the Clerk token getter with the API client.
 * Mount once inside ClerkProvider so axios can attach
 * Bearer tokens to every request automatically.
 */
export default function AuthProvider({ children }) {
  const { getToken, isLoaded, isSignedIn } = useAuth()

  useEffect(() => {
    if (isLoaded) {
      registerTokenGetter(async () => {
        if (!isSignedIn) return null
        const token = await getToken()
        console.log(
          'Token fetched:',
          token ? token.substring(0, 20) + '...' : 'null'
        )
        return token
      })
    }
  }, [isLoaded, isSignedIn, getToken])

  return children
}
