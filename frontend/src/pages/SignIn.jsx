import { SignIn } from '@clerk/clerk-react'

export default function SignInPage() {
  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-950">
      <SignIn
        appearance={{
          layout: { logoImageUrl: '/rook-lockup.png', logoPlacement: 'inside' },
          elements: {
            rootBox: 'mx-auto',
            card: 'bg-gray-900 border border-gray-800',
            headerTitle: 'text-white',
            headerSubtitle: 'text-gray-400',
            formButtonPrimary: 'bg-brand hover:bg-brand-hover text-white',
            formFieldLabel: 'text-gray-300',
            formFieldInput: 'bg-gray-800 border-gray-700 text-white',
            footerActionLink: 'text-brand-accent',
            identityPreviewText: 'text-gray-300',
            identityPreviewEditButton: 'text-brand-accent',
          },
        }}
      />
    </div>
  )
}
