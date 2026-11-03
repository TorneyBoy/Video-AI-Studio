"""Retired historical M1 entry point; use the complete source release tool."""
import argparse


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output')
    parser.parse_args()
    parser.error('M1 packaging is retired. Use: python scripts/package_release.py build --label <label> --output-directory <new-directory>')


if __name__=='__main__':main()
