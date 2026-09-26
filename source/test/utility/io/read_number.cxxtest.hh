// -*- mode:c++;tab-width:2;indent-tabs-mode:t;show-trailing-whitespace:t;rm-trailing-spaces:t -*-
// vi: set ts=2 noet:
//
// (c) Copyright Rosetta Commons Member Institutions.
// (c) This file is part of the Rosetta software suite and is made available under license.
// (c) The Rosetta software is developed by the contributing members of the Rosetta Commons.
// (c) For more information, see http://www.rosettacommons.org. Questions about this can be
// (c) addressed to University of Washington CoMotion, email: license@uw.edu.

/// @file   utility/io/read_number.cxxtest.hh
/// @brief  utility::io::read_number() gives what operator>> gives.
/// @details  Natively read_number() is operator>>. Under WebAssembly it has a parser of its own, and
/// that is what these tests are for.

// Package headers
#include <cxxtest/TestSuite.h>
#include <utility/io/util.hh>

// C++ headers
#include <cstddef>
#include <cstring>
#include <sstream>
#include <string>

namespace read_number_tests {

// Compare bytes, not values: -0.0 == 0.0, and read_number() has to give -0.0 where operator>> does.
template< typename T >
bool
same_bits( T const a, T const b ) {
	return std::memcmp( &a, &b, sizeof( T ) ) == 0;
}

/// @brief Read text once with operator>> and once with read_number(), and compare the value and the stream state.
template< typename T >
void
check_like_extraction( std::string const & text ) {
	std::istringstream extracted( text ), read( text );
	T extracted_value( 7 ), read_value( 7 );
	extracted >> extracted_value;
	utility::io::read_number( read, read_value );
	TSM_ASSERT( "value of '" + text + "'", same_bits( extracted_value, read_value ) );
	TSM_ASSERT_EQUALS( "failbit after '" + text + "'", extracted.fail(), read.fail() );
	TSM_ASSERT_EQUALS( "eofbit after '" + text + "'", extracted.eof(), read.eof() );
}

}  // namespace read_number_tests

using namespace read_number_tests;

class ReadNumberTests : public CxxTest::TestSuite {

public:

	/// @brief A line of lys.bbdep.rotamers.lib, read in the order RotamericSingleResidueDunbrackLibraryParser reads it.
	void test_reads_a_rotamer_library_line_like_extraction() {
		std::string const text =
			"LYS  -180 -180     5     1  2  2  2  4.622104E-001  7.717351E-001    62.6  -178.4  -179.6   180.0       7.1     9.5    12.0    11.4\n"
			"LYS  -180 -180     5     1  2  1  2  1.277255E-001  2.057872E+000    63.2   179.8    70.7   173.4       9.5    13.5    16.2    16.0\n";
		std::istringstream extracted( text ), read( text );
		for ( int line = 1; line <= 2; ++line ) {
			std::string extracted_code, read_code;
			extracted >> extracted_code;
			read >> read_code;
			TS_ASSERT_EQUALS( extracted_code, "LYS" );
			TS_ASSERT_EQUALS( read_code, "LYS" );
			for ( int column = 1; column <= 17; ++column ) {
				if ( column >= 3 && column <= 7 ) {
					std::size_t extracted_value( 0 ), read_value( 0 );
					extracted >> extracted_value;
					utility::io::read_number( read, read_value );
					TS_ASSERT_EQUALS( extracted_value, read_value );
				} else {
					double extracted_value( 0 ), read_value( 0 );
					extracted >> extracted_value;
					utility::io::read_number( read, read_value );
					TS_ASSERT( same_bits( extracted_value, read_value ) );
				}
			}
			// The newline is left for the next extraction to skip, as operator>> leaves it.
			TS_ASSERT_EQUALS( extracted.peek(), '\n' );
			TS_ASSERT_EQUALS( read.peek(), '\n' );
		}
		TS_ASSERT( read.good() );
	}

	/// @brief The semi-rotameric reader reads probabilities, means and deviations as float.
	void test_reads_floats_like_extraction() {
		check_like_extraction< float >( "4.622104E-001" );
		check_like_extraction< float >( "7.717351E-001" );
		check_like_extraction< float >( "-178.4" );
		check_like_extraction< float >( "0.1" );
		// Read through double: 10^-11, 10^11 and a significand over 2^24 are not exact in float.
		check_like_extraction< float >( "8.887795E-005" );
		check_like_extraction< float >( "17e11" );
		check_like_extraction< float >( "16777217e-1" );
		// The double nearest this is halfway between two floats, so rounding it again gives the wrong one.
		check_like_extraction< float >( "4.177937382873498e+16" );
		check_like_extraction< float >( "3.4028235e38" );
		check_like_extraction< float >( "1.4e-45" );
		check_like_extraction< float >( "-0.0" );
	}

	void test_reads_doubles_like_extraction() {
		check_like_extraction< double >( "1.726e-002" );
		check_like_extraction< double >( "0.1" );
		check_like_extraction< double >( "0.05" );
		check_like_extraction< double >( "-0.001" );
		check_like_extraction< double >( "-0.0" );
		check_like_extraction< double >( "+5" );
		check_like_extraction< double >( ".5" );
		check_like_extraction< double >( "5." );
		// 2^53 is the largest significand, and 1e22 the largest power of ten, exact in double.
		check_like_extraction< double >( "9007199254740992e-2" );
		check_like_extraction< double >( "9007199254740993e-2" );  // two roundings go wrong on this one
		check_like_extraction< double >( "1e22" );
		check_like_extraction< double >( "1e23" );
		check_like_extraction< double >( "123456789012345e-22" );
		check_like_extraction< double >( "3e-23" );
		check_like_extraction< double >( "1e-400" );
		check_like_extraction< double >( "0x1p3" );
		// Longer than read_number()'s token buffer, whose first 63 characters alone would read as 0.
		check_like_extraction< double >( std::string( 70, '0' ) + "1.5" );
		check_like_extraction< double >( std::string( 4093, '0' ) + "1.5" );  // 4,096 characters, the most it keeps
	}

	void test_reads_unsigned_integers_like_extraction() {
		check_like_extraction< std::size_t >( "0" );
		check_like_extraction< std::size_t >( "007" );
		check_like_extraction< std::size_t >( "4294967295" );
		check_like_extraction< std::size_t >( "4294967296" );
		check_like_extraction< std::size_t >( "18446744073709551615" );
		check_like_extraction< std::size_t >( "18446744073709551616" );
		check_like_extraction< std::size_t >( "-5" );
	}

	void test_sets_failbit_and_eofbit_like_extraction_when_no_number_is_left() {
		check_like_extraction< double >( "" );
		check_like_extraction< double >( " \t\n" );
	}

	void test_sets_eofbit_like_extraction_after_a_number_at_the_end() {
		check_like_extraction< double >( "1.5" );
		check_like_extraction< double >( "  1.5\n" );
		check_like_extraction< double >( "\r\v\f\t1.5\r\n" );
	}

	/// @brief On a token that is not a number, both fail. operator>> stops inside the token; read_number()
	/// under WebAssembly reads it whole, so eofbit is not compared.
	void test_fails_like_extraction_on_a_word() {
		std::istringstream extracted( "LYS" ), read( "LYS" );
		double extracted_value( 7 ), read_value( 7 );
		extracted >> extracted_value;
		utility::io::read_number( read, read_value );
		TS_ASSERT( extracted.fail() );
		TS_ASSERT( read.fail() );
		TS_ASSERT( same_bits( extracted_value, read_value ) );
	}

	/// @brief The documented difference: under WebAssembly a number must end at whitespace.
	void test_fails_on_a_number_followed_by_other_characters_under_webassembly() {
		std::istringstream read( "1.5) 2" );
		double value( 0 );
		utility::io::read_number( read, value );
		TS_ASSERT_EQUALS( value, 1.5 );
#ifdef __EMSCRIPTEN__
		TS_ASSERT( read.fail() );
#else
		TS_ASSERT( ! read.fail() );
#endif
	}

	/// @brief Under WebAssembly a token over 4,096 characters fails, so that one endless token cannot exhaust memory.
	void test_fails_on_a_token_over_4096_characters_under_webassembly() {
		std::istringstream read( std::string( 4094, '0' ) + "1.5 2" );
		double value( 7 );
		utility::io::read_number( read, value );
#ifdef __EMSCRIPTEN__
		TS_ASSERT( read.fail() );
		TS_ASSERT_EQUALS( value, 0.0 );
#else
		TS_ASSERT( ! read.fail() );
		TS_ASSERT_EQUALS( value, 1.5 );
#endif
	}

	/// @brief A NUL is one of those other characters, even in a token longer than read_number()'s buffer.
	void test_fails_on_a_nul_inside_a_number_under_webassembly() {
		std::istringstream short_token( std::string( "1.5\0xyz 2", 9 ) );
		std::istringstream long_token( "1" + std::string( 1, '\0' ) + std::string( 61, 'x' ) + "5" );
		double short_value( 0 ), long_value( 0 );
		utility::io::read_number( short_token, short_value );
		utility::io::read_number( long_token, long_value );
		TS_ASSERT_EQUALS( short_value, 1.5 );
		TS_ASSERT_EQUALS( long_value, 1.0 );
#ifdef __EMSCRIPTEN__
		TS_ASSERT( short_token.fail() );
		TS_ASSERT( long_token.fail() );
#else
		TS_ASSERT( ! short_token.fail() );
		TS_ASSERT( ! long_token.fail() );
#endif
	}

};
