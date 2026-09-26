// -*- mode:c++;tab-width:2;indent-tabs-mode:t;show-trailing-whitespace:t;rm-trailing-spaces:t -*-
// vi: set ts=2 noet:
//
// (c) Copyright Rosetta Commons Member Institutions.
// (c) This file is part of the Rosetta software suite and is made available under license.
// (c) The Rosetta software is developed by the contributing members of the Rosetta Commons.
// (c) For more information, see http://www.rosettacommons.org. Questions about this can be
// (c) addressed to University of Washington CoMotion, email: license@uw.edu.

/// @file   utility/io/util.hh
/// @brief  General database input/output utility functions


#ifndef INCLUDED_utility_io_util_hh
#define INCLUDED_utility_io_util_hh

#include <utility/io/ozstream.hh>
#include <utility/vector1.hh>

#include <cstddef>
#include <istream>

namespace utility {
namespace io {

template< class T >
void
read_vector( std::istream & is, vector1< T > & vec )
{
	vec.clear();
	T val;
	while ( is >> val ) {
		vec.push_back( val );
	}
}

template< class T >
void
write_vector( std::ostream & out, vector1< T > const & vec )
{
	for ( typename vector1< T >::const_iterator it = vec.begin(), eit = vec.end(); it != eit; ++it ) {
		out << *it << "\n";
	}
}

template< class T >
void
write_vector( std::string filename, vector1< T > const & vec )
{
	utility::io::ozstream out( filename );
	write_vector( out, vec );
}

/// @brief  General method that opens a file and returns its data as a list of lines after checking for errors.
utility::vector1< std::string > get_lines_from_file_data( std::string const & filename );

/// @brief  General method for removing comments from a line read from a database file.
void remove_inline_comments( std::string & line );

/// @brief  Read the next whitespace-separated number from a stream: the value `in >> value` gives.
/// @details  Under WebAssembly operator>> is slow. libc++ consults the stream's locale for every
/// character, and musl's strtod computes in software-emulated 128-bit long double. There these read the
/// token straight from the stream buffer and convert it themselves where that is exact: an integer digit
/// by digit, and a real number with one IEEE double multiply or divide (Clinger's fast path). Any other
/// token goes to operator>>. Native builds may use -ffast-math, under which that arithmetic is not
/// exact, so there these are operator>>.
///
/// Under WebAssembly the stream state follows operator>>, with one difference: these always read a whole
/// token. So "1.5)" fails, where operator>> reads 1.5 and leaves ")" for the next read, and a token that
/// is not a number is consumed whole. A token over 4,096 characters fails too, with value 0. They assume
/// the "C" locale's number format and decimal integers, as Rosetta's file streams use. They also ignore
/// noskipws and do not flush a tied stream.
std::istream & read_number( std::istream & in, double & value );
std::istream & read_number( std::istream & in, float & value );
std::istream & read_number( std::istream & in, std::size_t & value );

}  // namespace io
}  // namespace utility

#endif  // INCLUDED_utility_io_util_hh
